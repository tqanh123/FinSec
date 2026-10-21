#!/usr/bin/env node
// The `typescript` engine's Node side: one process per snapshot, JSON lines on stdio.
//
//   node ts_bridge.cjs --typescript <absolute dir of the harness-pinned typescript package>
//
//   {"op": "hello"}                              -> {"ts_version", "node_version"}
//   {"op": "read", "files": [{"path", "text"}]}  -> {"files": [{"imports", "symbols", "dups",
//                                                    "problems"}]}  (same order)
//   {"op": "configs", "paths": [abs tsconfig]}   -> {"configs": [{"extends", "errors", "options",
//                                                    "files", "include", "exclude", "references"}]}
//      the compiler's own reading of each config, `extends` applied: enums by name, paths
//      absolute, include/exclude/files as its validated specs (relative to the config's
//      directory, or absolute), `paths_base_dir` its pathsBasePath
//   {"op": "tree", "root", "files": [rel], "workspaces": {name: rel dir}} -> {"files": n}
//      the tree `resolve` answers from (once per process)
//   {"op": "resolve", "requests": [{"from", "spec", "mode": "import"|"require", "config": rel|null,
//                                   "mr"}]}       -> {"resolved": [{"fileName" (rel), "extension",
//                                                    "external", "packageId"} | null]}
//      `ts.resolveModuleName` with the config's options, `moduleResolution` set to `mr`,
//      over a host that sees only `tree`'s files (case-sensitive) and reads only them: a
//      real `node_modules` does not exist, and `<dir>/node_modules/<workspace name>/rest`
//      is that package's directory -- the answer never depends on an install
//   {"op": "alias_ir", "files": [{"path", "text"}]} -> {"trees": [node]}
//      a bundler config as the tree-sitter grammar names its nodes, for the subset the alias
//      evaluator (`aliases.py`) reads: {"t": type, "x": source text, "f": {field: node},
//      "c": [named children], "k": [anonymous tokens]}; anything else is {"t": "other"}
//   anything else                                -> {"error": "..."}
//
// The compiler is `require`d by the absolute path the caller passes, never by a bare
// name: Node resolves a bare name from THIS file's directory upward, and under a
// per-project install this file sits inside the project's tree, next to its own
// node_modules.
//
// The payload has the tree-sitter reader's shape (`treesitter/reader.py`), so the two
// engines can be compared record for record. Lines count `\n` only (a U+2028/U+2029 or a
// lone `\r` is no line break here, as in the kernel and tree-sitter -- TypeScript's own
// line map counts them); a column is the UTF-8 byte offset from the line start.
//
// Body hash `a1`: a pre-order walk over `ts.forEachChild`, emitting each node's
// SyntaxKind name, the property name it hangs from, and leaf text, after:
//   1. comments and JSDoc never enter (forEachChild does not visit them);
//   2. a parenthesized expression/type is replaced by its one child;
//   3. a union/intersection nested in one of the same kind is flattened;
//   4. a string-like literal is its value (quote style folded); a number its value
//      (`1_000` == `1000`, as the compiler normalizes it);
//   5. JSX text is its non-empty trimmed lines joined by one space, whitespace-only
//      JSX text and an empty `{}` / comment-only expression dropped;
//   6. the `export` / `default` modifiers of the hashed node itself are dropped (an
//      export added is no body change);
//   7. the semantic scalars an AST keeps outside its children go in: a unary/type
//      operator, `import.meta`'s keyword, `type`-only and `export =` flags, a
//      variable list's let/const/using.
// The input starts with `typescript`, the compiler version and `a1`; the result reads
// `typescript:<version>+a1:<hex16>`. An empty arrow parameter list and its parentheses
// are one AST shape already (`a => b` == `(a) => b`).
"use strict";

const crypto = require("crypto");
const path = require("path");
const readline = require("readline");

const ALGO = "a1";
const SEP = "\x1f";
const MAX_PROBLEMS = 50;

function argValue(flag) {
  const i = process.argv.indexOf(flag);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

const tsDir = argValue("--typescript");
if (!tsDir || !path.isAbsolute(tsDir)) {
  process.stderr.write("ts_bridge: --typescript <absolute path> is required\n");
  process.exit(2);
}
const ts = require(tsDir);
const K = ts.SyntaxKind;

// SyntaxKind has aliases (FirstLiteralToken == NumericLiteral): one stable name per value
const KIND_NAME = {};
for (const name of Object.keys(K)) {
  const v = K[name];
  if (typeof v !== "number") continue;
  const marker = /^(First|Last)[A-Z]/.test(name);
  if (KIND_NAME[v] === undefined || (KIND_NAME[v].marker && !marker)) KIND_NAME[v] = { name, marker };
}
function kindName(kind) {
  return KIND_NAME[kind] ? KIND_NAME[kind].name : String(kind);
}

// ------------------------------------------------------------------ positions

function lineIndex(text) {
  const starts = [0];
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) starts.push(i + 1);
  return starts;
}

function lineOf(starts, pos) {
  let lo = 0;
  let hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= pos) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

function point(ctx, pos) {
  const row = lineOf(ctx.starts, pos);
  const col = Buffer.byteLength(ctx.text.slice(ctx.starts[row], pos), "utf8");
  return { line: row + 1, col };
}

function startOf(ctx, node) {
  return node.getStart(ctx.sf);
}

function lines(ctx, node) {
  return [point(ctx, startOf(ctx, node)).line, point(ctx, node.end).line];
}

// ------------------------------------------------------------------ helpers

function hasModifier(node, kind) {
  return !!(node.modifiers && node.modifiers.some((m) => m.kind === kind));
}

const FUNCTIONS = new Set([K.FunctionDeclaration, K.FunctionExpression, K.ArrowFunction,
  K.MethodDeclaration, K.Constructor, K.GetAccessor, K.SetAccessor, K.ClassStaticBlockDeclaration]);

function inFunction(node) {
  for (let n = node.parent; n; n = n.parent) if (FUNCTIONS.has(n.kind)) return true;
  return false;
}

function isStringLike(node) {
  return node && (node.kind === K.StringLiteral || node.kind === K.NoSubstitutionTemplateLiteral);
}

// the tree-sitter node type a non-literal specifier would carry, so both engines name it alike
const EXPR_KIND = {
  [K.Identifier]: "identifier", [K.BinaryExpression]: "binary_expression",
  [K.TemplateExpression]: "template_string", [K.NoSubstitutionTemplateLiteral]: "template_string",
  [K.CallExpression]: "call_expression", [K.PropertyAccessExpression]: "member_expression",
  [K.ElementAccessExpression]: "subscript_expression", [K.ConditionalExpression]: "ternary_expression",
  [K.ParenthesizedExpression]: "parenthesized_expression", [K.AwaitExpression]: "await_expression",
};

function snake(kind) {
  return kindName(kind).replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
}

function specOf(ctx, arg) {
  if (!arg) return [null, { kind: "none", text: "" }];
  if (arg.kind === K.StringLiteral) return [arg.text, null];
  return [null, { kind: EXPR_KIND[arg.kind] || snake(arg.kind), text: arg.getText(ctx.sf) }];
}

function nameText(node) {
  return node.text !== undefined ? node.text : node.escapedText;
}

// ------------------------------------------------------------------ imports

function importBindings(stmt) {
  const clause = stmt.importClause;
  if (!clause) return [];
  const stmtType = !!clause.isTypeOnly;
  const out = [];
  if (clause.name) out.push(["default", clause.name.text, stmtType]);
  const nb = clause.namedBindings;
  if (nb && nb.kind === K.NamespaceImport) out.push(["*", nb.name.text, stmtType]);
  if (nb && nb.kind === K.NamedImports) {
    for (const el of nb.elements) {
      const imported = nameText(el.propertyName || el.name);
      out.push([imported, nameText(el.name), stmtType || !!el.isTypeOnly]);
    }
  }
  return out;
}

function exportBindings(stmt) {
  const clause = stmt.exportClause;
  const stmtType = !!stmt.isTypeOnly;
  if (!clause) return [];
  if (clause.kind === K.NamespaceExport) return [["*", nameText(clause.name), stmtType]];
  return clause.elements.map((el) =>
    [nameText(el.propertyName || el.name), nameText(el.name), stmtType || !!el.isTypeOnly]);
}

function imports(ctx) {
  const out = [];
  const add = (node, rec) => out.push([startOf(ctx, node), Object.assign(
    { line: point(ctx, startOf(ctx, node)).line, in_function: inFunction(node) }, rec)]);
  const visit = (node) => {
    switch (node.kind) {
      case K.ImportDeclaration:
        if (isStringLike(node.moduleSpecifier)) {
          add(node, { kind: node.importClause ? "static" : "side_effect", spec: node.moduleSpecifier.text,
            bindings: importBindings(node), type_only: !!(node.importClause && node.importClause.isTypeOnly) });
        }
        break;
      case K.ExportDeclaration:
        if (node.moduleSpecifier && isStringLike(node.moduleSpecifier)) {
          const named = node.exportClause && node.exportClause.kind === K.NamedExports;
          add(node, { kind: named ? "export_from" : "export_all", spec: node.moduleSpecifier.text,
            bindings: exportBindings(node), type_only: !!node.isTypeOnly });
        }
        break;
      case K.ImportEqualsDeclaration:
        if (node.moduleReference.kind === K.ExternalModuleReference &&
            isStringLike(node.moduleReference.expression)) {
          add(node, { kind: "import_equals", spec: node.moduleReference.expression.text,
            bindings: [["*", node.name.text, false]], type_only: !!node.isTypeOnly });
        }
        break;
      case K.ImportType: {
        const lit = node.argument && node.argument.kind === K.LiteralType ? node.argument.literal : null;
        const [spec, expr] = specOf(ctx, lit);
        add(node, Object.assign({ kind: "dynamic", spec, bindings: [], type_only: true },
          expr ? { spec_expr: expr } : {}));
        break;
      }
      case K.CallExpression: {
        const callee = node.expression;
        if (callee.kind === K.ImportKeyword) {
          const [spec, expr] = specOf(ctx, node.arguments[0]);
          add(node, Object.assign({ kind: "dynamic", spec, bindings: [],
            type_only: !!(node.parent && node.parent.kind === K.TypeOfExpression) },
          expr ? { spec_expr: expr } : {}));
        } else if (callee.kind === K.Identifier && callee.text === "require" && node.arguments.length) {
          const [spec, expr] = specOf(ctx, node.arguments[0]);
          add(node, Object.assign({ kind: "require", spec, bindings: [], type_only: false },
            expr ? { spec_expr: expr } : {}));
        }
        break;
      }
      default:
        break;
    }
    ts.forEachChild(node, visit);
  };
  visit(ctx.sf);
  out.sort((a, b) => a[0] - b[0]);
  return out.map((x) => x[1]);
}

// ------------------------------------------------------------------ symbols

const PARENS = new Set([K.ParenthesizedExpression, K.ParenthesizedType]);
const FLAT = new Set([K.UnionType, K.IntersectionType]);
const LEAF_TEXT = new Set([K.Identifier, K.PrivateIdentifier, K.StringLiteral, K.NumericLiteral,
  K.BigIntLiteral, K.RegularExpressionLiteral, K.NoSubstitutionTemplateLiteral, K.TemplateHead,
  K.TemplateMiddle, K.TemplateTail]);
const SCALARS = ["operator", "keywordToken", "isTypeOnly", "isExportEquals", "isTypeOf"];
const BLOCK_FLAGS = ts.NodeFlags.Let | ts.NodeFlags.Const | ts.NodeFlags.Using;

function unwrap(node) {
  while (PARENS.has(node.kind)) node = node.type || node.expression;
  return node;
}

function childrenOf(node) {
  // [field, child] in source order; the field is the property the child hangs from
  const keys = Object.keys(node);
  const fieldOf = (value) => keys.find((k) => k !== "parent" && node[k] === value) || "";
  const out = [];
  ts.forEachChild(node,
    (c) => { out.push([fieldOf(c), c]); },
    (arr) => { const f = fieldOf(arr); for (const c of arr) out.push([f, c]); });
  return out;
}

function emit(node, field, tokens, skipOwn) {
  node = unwrap(node);
  if (node.kind === K.JsxText) {
    const t = node.text.split("\n").map((s) => s.trim()).filter(Boolean).join(" ");
    if (t) tokens.push(field, "JsxText", t);
    return;
  }
  if (node.kind === K.JsxExpression && !node.expression) return;
  tokens.push(field, kindName(node.kind));
  if (LEAF_TEXT.has(node.kind)) tokens.push(nameText(node));
  for (const s of SCALARS) if (node[s] !== undefined) tokens.push(s + "=" + String(node[s]));
  if (node.kind === K.VariableDeclarationList) tokens.push("flags=" + (node.flags & BLOCK_FLAGS));
  for (const [f, c] of childrenOf(node)) {
    if (skipOwn && f === "modifiers" && (c.kind === K.ExportKeyword || c.kind === K.DefaultKeyword)) continue;
    const u = unwrap(c);
    if (FLAT.has(node.kind) && u.kind === node.kind) {
      for (const [f2, c2] of childrenOf(u)) emit(c2, f2, tokens, false);
      continue;
    }
    emit(u, f, tokens, false);
  }
  tokens.push(")");
}

function sha(ctx, node, lead) {
  const tokens = ["typescript", ctx.version, ALGO].concat(lead || []);
  emit(node, "", tokens, true);
  return "typescript:" + ctx.version + "+" + ALGO + ":" +
    crypto.createHash("sha256").update(tokens.join(SEP), "utf8").digest("hex").slice(0, 16);
}

function sym(ctx, name, kind, node, hashNode, lead, extra) {
  return Object.assign({ name, kind, lines: lines(ctx, node), sha: sha(ctx, hashNode || node, lead) },
    extra || {});
}

function methods(ctx, cls) {
  const parent = cls.name ? cls.name.getText(ctx.sf) : null;
  const out = [];
  for (const m of cls.members) {
    const isMethod = (m.kind === K.MethodDeclaration && (m.body || hasModifier(m, K.AbstractKeyword))) ||
      m.kind === K.Constructor && m.body || m.kind === K.GetAccessor || m.kind === K.SetAccessor;
    if (!isMethod) continue;
    const name = m.kind === K.Constructor ? "constructor" : m.name.getText(ctx.sf);
    out.push(sym(ctx, name, "method", m, m, null, { parent }));
  }
  return out;
}

function bases(ctx, cls) {
  const out = [];
  for (const h of cls.heritageClauses || []) {
    if (h.token === K.ExtendsKeyword) for (const t of h.types) out.push(t.expression.getText(ctx.sf));
  }
  return out;
}

function namespaceName(node) {
  const parts = [node.name.text];
  let body = node.body;
  while (body && body.kind === K.ModuleDeclaration) {
    parts.push(body.name.text);
    body = body.body;
  }
  return parts.join(".");
}

function declarationSymbols(ctx, stmt, exported) {
  const declared = hasModifier(stmt, K.DeclareKeyword);
  const def = hasModifier(stmt, K.DefaultKeyword);
  switch (stmt.kind) {
    case K.FunctionDeclaration:
      if (declared && !exported) return [];
      if (!stmt.name) return def ? [sym(ctx, "default", "func", stmt)] : [];
      return [sym(ctx, stmt.name.text, "func", stmt)];
    case K.ClassDeclaration:
      if (declared && !exported) return [];
      if (!stmt.name) return def ? [sym(ctx, "default", "class", stmt)] : [];
      return [sym(ctx, stmt.name.text, "class", stmt, stmt, null,
        { methods: methods(ctx, stmt), bases: bases(ctx, stmt) })];
    case K.VariableStatement: {
      const flags = stmt.declarationList.flags;
      if (declared && (!exported || !(flags & BLOCK_FLAGS))) return [];
      const keyword = flags & ts.NodeFlags.Using ? (flags & ts.NodeFlags.Const ? "await" : "using")
        : flags & ts.NodeFlags.Const ? "const" : flags & ts.NodeFlags.Let ? "let" : "var";
      return stmt.declarationList.declarations.filter((d) => d.name.kind === K.Identifier)
        .map((d) => sym(ctx, d.name.text, "const", d, d, [keyword]));
    }
    case K.InterfaceDeclaration:
      return declared && !exported ? [] : [sym(ctx, stmt.name.text, "interface", stmt)];
    case K.TypeAliasDeclaration:
      return declared && !exported ? [] : [sym(ctx, stmt.name.text, "type", stmt)];
    case K.EnumDeclaration:
      return declared ? [] : [sym(ctx, stmt.name.text, "enum", stmt)];
    case K.ModuleDeclaration:
      if (declared || stmt.name.kind !== K.Identifier || !(stmt.flags & ts.NodeFlags.Namespace)) return [];
      return [sym(ctx, namespaceName(stmt), "namespace", stmt)];
    default:
      return [];
  }
}

function valueKind(v) {
  return v.kind === K.FunctionExpression || v.kind === K.ArrowFunction ? "func"
    : v.kind === K.ClassExpression ? "class" : "const";
}

// CommonJS: `module.exports = {a, b: f}` names each key, `module.exports = v` is
// `default`, `exports.x = v` / `module.exports.x = v` names `x` (the tree-sitter reader's rule)
function cjsSymbols(ctx, stmt) {
  const e = stmt.expression;
  if (!e || e.kind !== K.BinaryExpression || e.operatorToken.kind !== K.EqualsToken ||
      e.left.kind !== K.PropertyAccessExpression) return [];
  const path = e.left.getText(ctx.sf).replace(/\s+/g, "");
  const v = e.right;
  if (path === "module.exports") {
    if (v.kind !== K.ObjectLiteralExpression) return [sym(ctx, "default", valueKind(v), stmt, v)];
    const out = [];
    for (const p of v.properties) {
      if (p.kind === K.PropertyAssignment && (p.name.kind === K.Identifier || p.name.kind === K.StringLiteral)) {
        out.push(sym(ctx, p.name.text, valueKind(p.initializer), p, p.initializer));
      } else if (p.kind === K.ShorthandPropertyAssignment) {
        out.push(sym(ctx, p.name.text, "const", p));
      } else if (p.kind === K.MethodDeclaration && p.name.kind === K.Identifier) {
        out.push(sym(ctx, p.name.text, "func", p));
      }
    }
    return out;
  }
  for (const head of ["module.exports.", "exports."]) {
    const name = path.startsWith(head) ? path.slice(head.length) : "";
    if (/^[A-Za-z_$][\w$]*$/.test(name)) return [sym(ctx, name, valueKind(v), stmt, v)];
  }
  return [];
}

function symbols(ctx) {
  const found = [];
  for (const stmt of ctx.sf.statements) {
    if (stmt.kind === K.ExportAssignment) {
      if (stmt.isExportEquals) continue;
      const v = stmt.expression;
      found.push(sym(ctx, "default", valueKind(v), stmt, v));
      continue;
    }
    if (stmt.kind === K.ExpressionStatement) {
      for (const s of cjsSymbols(ctx, stmt)) found.push(s);
      continue;
    }
    for (const s of declarationSymbols(ctx, stmt, hasModifier(stmt, K.ExportKeyword))) found.push(s);
  }
  const seen = new Set();
  const dups = [];
  const syms = [];
  for (const s of found) {
    if (seen.has(s.name)) {
      if (!dups.includes(s.name)) dups.push(s.name);
      continue;
    }
    seen.add(s.name);
    syms.push(s);
  }
  return [syms, dups];
}

// ------------------------------------------------------------------ problems

const TS_ONLY = new Set([K.InterfaceDeclaration, K.TypeAliasDeclaration, K.EnumDeclaration,
  K.ModuleDeclaration, K.AsExpression, K.SatisfiesExpression, K.NonNullExpression,
  K.TypeAssertionExpression]);
const TS_MODIFIERS = new Set([K.PublicKeyword, K.PrivateKeyword, K.ProtectedKeyword,
  K.AbstractKeyword, K.DeclareKeyword, K.ReadonlyKeyword]);

function tsSyntaxInJs(ctx) {
  let hit = null;
  const visit = (node) => {
    if (hit) return;
    const typed = node.type && node.kind !== K.TypeOfExpression && ts.isTypeNode(node.type);
    if (TS_ONLY.has(node.kind) || typed || (node.modifiers || []).some((m) => TS_MODIFIERS.has(m.kind)) ||
        (node.kind === K.HeritageClause && node.token === K.ImplementsKeyword)) {
      hit = node;
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(ctx.sf);
  return hit ? Object.assign({ kind: "ts_syntax_in_js" }, point(ctx, startOf(ctx, hit))) : null;
}

function problems(ctx, js) {
  const out = (ctx.sf.parseDiagnostics || []).map((d) => Object.assign({ kind: "syntax" },
    point(ctx, d.start || 0), { message: ts.flattenDiagnosticMessageText(d.messageText, "\n"), code: d.code }));
  out.sort((a, b) => a.line - b.line || a.col - b.col);
  if (js) {
    const hit = tsSyntaxInJs(ctx);
    if (hit) out.push(hit);
  }
  return out.slice(0, MAX_PROBLEMS);
}

// ------------------------------------------------------------------ files

function scriptKind(file) {
  const name = file.toLowerCase();
  if (name.endsWith(".tsx")) return [ts.ScriptKind.TSX, false];
  if (/\.(ts|mts|cts)$/.test(name)) return [ts.ScriptKind.TS, false];
  if (name.endsWith(".jsx")) return [ts.ScriptKind.JSX, true];
  return [ts.ScriptKind.JS, true];
}

function readOne(file) {
  try {
    const [kind, js] = scriptKind(file.path);
    const sf = ts.createSourceFile(file.path, file.text, ts.ScriptTarget.Latest, true, kind);
    const ctx = { sf, text: file.text, starts: lineIndex(file.text), version: ts.version };
    const [syms, dups] = symbols(ctx);
    return { imports: imports(ctx), symbols: syms, dups, problems: problems(ctx, js) };
  } catch (e) {
    return { imports: [], symbols: [], dups: [],
      problems: [{ kind: "EXCEPTION", line: 0, col: 0, detail: String(e && e.name || e) }] };
  }
}

// ------------------------------------------------------------------ configs

function enumName(E, v) {
  for (const k of Object.keys(E)) if (E[k] === v && isNaN(Number(k))) return k.toLowerCase();
  return String(v);
}

const CONFIG_BOOLS = ["allowJs", "resolveJsonModule", "allowImportingTsExtensions", "composite"];
const CONFIG_PATHS = ["baseUrl", "rootDir", "outDir", "declarationDir"];

function readConfig(p) {
  try {
    const sf = ts.readJsonConfigFile(p, ts.sys.readFile);
    const pc = ts.parseJsonSourceFileConfigFileContent(sf, ts.sys, path.dirname(p), undefined, p);
    const o = pc.options;
    const specs = (o.configFile && o.configFile.configFileSpecs) || {};
    const opts = {};
    if (o.moduleResolution !== undefined) opts.moduleResolution = enumName(ts.ModuleResolutionKind, o.moduleResolution);
    if (o.module !== undefined) opts.module = enumName(ts.ModuleKind, o.module);
    if (o.target !== undefined) opts.target = enumName(ts.ScriptTarget, o.target);
    for (const k of CONFIG_BOOLS) if (o[k] !== undefined) opts[k] = !!o[k];
    for (const k of CONFIG_PATHS) if (o[k] !== undefined) opts[k] = o[k];
    if (o.rootDirs !== undefined) opts.rootDirs = o.rootDirs;
    if (o.customConditions !== undefined) opts.customConditions = o.customConditions;
    if (o.paths !== undefined) {
      opts.paths = o.paths;
      opts.paths_base_dir = o.pathsBasePath;
    }
    if (o.moduleSuffixes !== undefined) opts.moduleSuffixes = true;
    return {
      extends: sf.extendedSourceFiles || [],
      errors: pc.errors.map((e) => ({ code: e.code, message: ts.flattenDiagnosticMessageText(e.messageText, "\n") })),
      options: opts,
      files: specs.filesSpecs ? specs.validatedFilesSpec || [] : null,
      include: specs.includeSpecs && !specs.isDefaultIncludeSpec ? specs.validatedIncludeSpecs || [] : null,
      exclude: specs.validatedExcludeSpecs || [],
      references: (pc.projectReferences || []).map((r) => r.path),
    };
  } catch (e) {
    return { error: String(e && e.message || e) };
  }
}

// ------------------------------------------------------------------ resolve

let TREE = null;
const PARSED = new Map();

function treeOp(req) {
  const files = new Set(req.files || []);
  const dirs = new Set();
  for (const f of files) {
    for (let d = path.posix.dirname(f); d !== "." && !dirs.has(d); d = path.posix.dirname(d)) dirs.add(d);
  }
  TREE = { root: req.root, files, dirs, ws: new Map(Object.entries(req.workspaces || {})) };
  PARSED.clear();
  return { files: files.size };
}

function relOf(p) {
  return path.relative(TREE.root, p).split(path.sep).join("/") || ".";
}

// a repo-relative path with every `node_modules/<workspace name>` replaced by that package's
// directory: {path} | {nm: true} (a node_modules dir) | {scope} (node_modules/@scope) | {none: true}
function mapped(rel) {
  for (let guard = 0; guard < 8; guard++) {
    const segs = rel.split("/");
    const i = segs.indexOf("node_modules");
    if (i < 0) return { path: rel };
    const rest = segs.slice(i + 1);
    if (!rest.length) return { nm: true };
    const n = rest[0].startsWith("@") ? 2 : 1;
    if (rest.length < n) return { scope: rest[0] };
    const dir = TREE.ws.get(rest.slice(0, n).join("/"));
    if (dir === undefined) return { none: true };
    rel = [dir === "." ? "" : dir].concat(rest.slice(n)).filter(Boolean).join("/") || ".";
  }
  return { none: true };
}

const HOST = {
  fileExists: (p) => { const m = mapped(relOf(p)); return m.path !== undefined && TREE.files.has(m.path); },
  directoryExists: (p) => {
    const m = mapped(relOf(p));
    if (m.nm) return TREE.ws.size > 0;
    if (m.scope) return [...TREE.ws.keys()].some((k) => k.startsWith(m.scope + "/"));
    return m.path !== undefined && (m.path === "." || TREE.dirs.has(m.path));
  },
  readFile: (p) => {
    const m = mapped(relOf(p));
    if (m.path === undefined || !TREE.files.has(m.path)) return undefined;
    try { return require("fs").readFileSync(path.join(TREE.root, m.path), "utf8"); } catch (e) { return undefined; }
  },
  realpath: (p) => { const m = mapped(relOf(p)); return m.path !== undefined ? path.join(TREE.root, m.path) : p; },
  getCurrentDirectory: () => TREE.root,
  getDirectories: () => [],
  useCaseSensitiveFileNames: true,
};

const MR = { bundler: "Bundler", node10: "Node10", node16: "Node16", nodenext: "NodeNext", classic: "Classic" };
const MODED = new Set(["bundler", "node16", "nodenext"]);

function optionsOf(config) {
  if (!config) return { allowJs: true };
  if (!PARSED.has(config)) {
    const abs = path.join(TREE.root, config);
    const sf = ts.readJsonConfigFile(abs, ts.sys.readFile);
    PARSED.set(config, ts.parseJsonSourceFileConfigFileContent(sf, ts.sys, path.dirname(abs), undefined, abs).options);
  }
  return PARSED.get(config);
}

function resolveOne(r) {
  const options = Object.assign({}, optionsOf(r.config), { moduleResolution: ts.ModuleResolutionKind[MR[r.mr]] });
  // the program passes a mode only where resolution has one: under node10 any mode turns
  // on every feature (`exports`, `imports`), which `tsc` never does there
  const mode = !MODED.has(r.mr) ? undefined : r.mode === "require" ? ts.ModuleKind.CommonJS : ts.ModuleKind.ESNext;
  const got = ts.resolveModuleName(r.spec, path.join(TREE.root, r.from), options, HOST, undefined, undefined, mode);
  const m = got.resolvedModule;
  if (!m) return null;
  const at = mapped(relOf(m.resolvedFileName));
  if (at.path === undefined || at.path.startsWith("../")) return null;
  return { fileName: at.path, extension: m.extension, external: !!m.isExternalLibraryImport,
    packageId: m.packageId ? m.packageId.name : null };
}

// ------------------------------------------------------------------ alias IR

const mk = (t, x, f, c, k) => ({ t, x, f: f || {}, c: c || [], k: k || [] });

function irNode(sf, n) {
  const x = n.getText(sf);
  const node = (t, f, c, k) => mk(t, x, f, c, k);
  const ir = (m) => (m ? irNode(sf, m) : undefined);
  const leaf = (t, text) => ({ t, x: text, f: {}, c: [], k: [] });
  switch (n.kind) {
    case K.SourceFile: return node("program", {}, n.statements.map(ir));
    case K.ImportDeclaration: {
      const c = [];
      const cl = n.importClause;
      if (cl) {
        const kids = [];
        if (cl.name) kids.push(leaf("identifier", cl.name.text));
        const nb = cl.namedBindings;
        if (nb && nb.kind === K.NamespaceImport) kids.push(node("namespace_import", {}, [leaf("identifier", nb.name.text)]));
        if (nb && nb.kind === K.NamedImports) {
          kids.push(node("named_imports", {}, nb.elements.map((e) => {
            const f = { name: leaf("identifier", (e.propertyName || e.name).text) };
            if (e.propertyName) f.alias = leaf("identifier", e.name.text);
            return { t: "import_specifier", x: e.getText(sf), f, c: Object.values(f), k: [] };
          })));
        }
        c.push(node("import_clause", {}, kids));
      }
      const src = ir(n.moduleSpecifier);
      return node("import_statement", { source: src }, c.concat([src]));
    }
    case K.VariableStatement:
      return node("lexical_declaration", {}, n.declarationList.declarations.map((d) => {
        let name = ir(d.name);
        if (d.name.kind === K.ObjectBindingPattern) {
          name = mk("object_pattern", d.name.getText(sf), {}, d.name.elements
            .filter((e) => !e.propertyName && !e.dotDotDotToken)
            .map((e) => leaf("shorthand_property_identifier_pattern", e.name.getText(sf))));
        }
        const f = { name };
        if (d.initializer) f.value = ir(d.initializer);
        return { t: "variable_declarator", x: d.getText(sf), f, c: Object.values(f), k: [] };
      }));
    case K.ExportAssignment:
      return n.isExportEquals ? node("other") : node("export_statement", { value: ir(n.expression) }, [ir(n.expression)],
        ["export", "default"]);
    case K.ExpressionStatement: return node("expression_statement", {}, [ir(n.expression)]);
    case K.BinaryExpression: {
      const left = ir(n.left), right = ir(n.right);
      if (n.operatorToken.kind === K.EqualsToken) return node("assignment_expression", { left, right }, [left, right]);
      const op = leaf("operator", n.operatorToken.getText(sf));
      return node("binary_expression", { left, operator: op, right }, [left, right]);
    }
    case K.StringLiteral: return node("string", {}, [leaf("string_fragment", n.text)]);
    case K.NoSubstitutionTemplateLiteral: return node("template_string", {}, [leaf("string_fragment", n.text)]);
    case K.TemplateExpression: {
      const c = [];
      if (n.head.text) c.push(leaf("string_fragment", n.head.text));
      for (const sp of n.templateSpans) {
        c.push(mk("template_substitution", "", {}, [ir(sp.expression)]));
        if (sp.literal.text) c.push(leaf("string_fragment", sp.literal.text));
      }
      return node("template_string", {}, c);
    }
    case K.Identifier: return leaf("identifier", n.text);
    case K.PropertyAccessExpression: {
      const object = ir(n.expression), property = leaf("property_identifier", n.name.text);
      return node("member_expression", { object, property }, [object, property]);
    }
    case K.MetaProperty: return leaf("meta_property", x);
    case K.CallExpression: {
      const fn = ir(n.expression), args = mk("arguments", "", {}, n.arguments.map(ir));
      return node("call_expression", { function: fn, arguments: args }, [fn, args]);
    }
    case K.NewExpression: {
      const ctor = ir(n.expression), args = mk("arguments", "", {}, (n.arguments || []).map(ir));
      return node("new_expression", { constructor: ctor, arguments: args }, [ctor, args]);
    }
    case K.ObjectLiteralExpression: return node("object", {}, n.properties.map(ir));
    case K.PropertyAssignment: {
      const nm = n.name;
      const key = nm.kind === K.Identifier ? leaf("property_identifier", nm.text)
        : nm.kind === K.ComputedPropertyName ? leaf("computed_property_name", nm.getText(sf)) : ir(nm);
      const value = ir(n.initializer);
      return node("pair", { key, value }, [key, value]);
    }
    case K.ShorthandPropertyAssignment: return leaf("shorthand_property_identifier", n.name.text);
    case K.SpreadAssignment: case K.SpreadElement: return node("spread_element", {}, [ir(n.expression)]);
    case K.MethodDeclaration: return node("method_definition", { name: leaf("property_identifier", n.name.getText(sf)) });
    case K.ArrayLiteralExpression: return node("array", {}, n.elements.map(ir));
    case K.RegularExpressionLiteral: {
      const cut = x.lastIndexOf("/");
      const f = { pattern: leaf("regex_pattern", x.slice(1, cut)) };
      if (x.slice(cut + 1)) f.flags = leaf("regex_flags", x.slice(cut + 1));
      return node("regex", f, Object.values(f));
    }
    case K.ParenthesizedExpression: return node("parenthesized_expression", {}, [ir(n.expression)]);
    case K.SatisfiesExpression: return node("satisfies_expression", {}, [ir(n.expression)]);
    case K.AsExpression: return node("as_expression", {}, [ir(n.expression)]);
    case K.NonNullExpression: return node("non_null_expression", {}, [ir(n.expression)]);
    case K.ArrowFunction: return node("arrow_function", { body: ir(n.body) });
    case K.FunctionExpression: return node("function_expression", { body: ir(n.body) });
    case K.TrueKeyword: return leaf("true", x);
    case K.FalseKeyword: return leaf("false", x);
    case K.NumericLiteral: return leaf("number", x);
    default: return node("other");
  }
}

// ------------------------------------------------------------------ refs

// `facts.Refs` of one file, the tree-sitter reader's `refs` in this AST's terms: an
// identifier the grammar would name `identifier` / `type_identifier` /
// `shorthand_property_identifier` is plain; `a.b` / `A.B` (type) / `<a.B>` a member; a
// property, member, label or attribute NAME none of them.
const NAMED = new Set([K.PropertyAssignment, K.PropertyDeclaration, K.PropertySignature, K.MethodDeclaration,
  K.MethodSignature, K.GetAccessor, K.SetAccessor, K.EnumMember, K.JsxAttribute]);
const LABELED = new Set([K.LabeledStatement, K.BreakStatement, K.ContinueStatement]);

function requireBound(name) {
  if (name.kind === K.Identifier) return [["*", name.text]];
  if (name.kind !== K.ObjectBindingPattern) return null;
  const out = [];
  for (const e of name.elements) {
    if (e.dotDotDotToken || e.name.kind !== K.Identifier) continue;
    const key = e.propertyName;
    if (!key) out.push([e.name.text, e.name.text]);
    else if (key.kind === K.Identifier || key.kind === K.StringLiteral) out.push([key.text, e.name.text]);
  }
  return out;
}

function refsOne(file) {
  const sf = ts.createSourceFile(file.path, file.text, ts.ScriptTarget.Latest, true, scriptKind(file.path)[0]);
  const line = (n) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1;
  const plain = [], member = [], requires = [];
  const visit = (n) => {
    if (n.kind === K.ImportDeclaration || (n.kind === K.ExportDeclaration && n.moduleSpecifier)
        || (n.kind === K.ImportEqualsDeclaration && n.moduleReference.kind === K.ExternalModuleReference)) return;
    if (n.kind === K.VariableDeclaration && n.initializer && n.initializer.kind === K.CallExpression) {
      const c = n.initializer, arg = c.arguments[0];
      const bound = requireBound(n.name);
      if (c.expression.kind === K.Identifier && c.expression.text === "require" && arg
          && arg.kind === K.StringLiteral && bound) requires.push([line(c), arg.text, bound]);
    }
    if (n.kind === K.Identifier) {
      const p = n.parent;
      if ((p.kind === K.PropertyAccessExpression && p.name === n) || (p.kind === K.QualifiedName && p.right === n)) {
        const obj = p.kind === K.QualifiedName ? p.left : p.expression;
        if (obj.kind === K.Identifier) member.push([line(obj), obj.text, n.text]);
        return;
      }
      if ((NAMED.has(p.kind) && p.name === n) || (LABELED.has(p.kind) && p.label === n)) return;
      if (p.kind === K.BindingElement && p.parent.kind === K.ObjectBindingPattern
          && (p.propertyName === n || !p.propertyName)) return;
      plain.push([line(n), n.text]);
      return;
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  const cmp = (a, b) => (a[0] - b[0]) || (String(a[1]) < String(b[1]) ? -1 : String(a[1]) > String(b[1]) ? 1 : 0)
    || (String(a[2]) < String(b[2]) ? -1 : String(a[2]) > String(b[2]) ? 1 : 0);
  return { plain: plain.sort(cmp), member: member.sort(cmp), requires: requires.sort(cmp) };
}

function answer(req) {
  if (req.op === "hello") return { ts_version: ts.version, node_version: process.versions.node };
  if (req.op === "read") return { files: (req.files || []).map(readOne) };
  if (req.op === "configs") return { configs: (req.paths || []).map(readConfig) };
  if (req.op === "tree") return treeOp(req);
  if (req.op === "alias_ir") return { trees: (req.files || []).map((f) => {
    const sf = ts.createSourceFile(f.path, f.text, ts.ScriptTarget.Latest, true, scriptKind(f.path)[0]);
    return irNode(sf, sf);
  }) };
  if (req.op === "refs") return { refs: (req.files || []).map(refsOne) };
  if (req.op === "resolve") {
    if (!TREE) return { error: "resolve before tree" };
    return { resolved: (req.requests || []).map(resolveOne) };
  }
  return { error: "unknown op " + JSON.stringify(req.op) };
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  if (!line.trim()) return;
  let resp;
  try {
    resp = answer(JSON.parse(line));
  } catch (e) {
    resp = { error: String(e && e.message || e) };
  }
  process.stdout.write(JSON.stringify(resp) + "\n");
});
