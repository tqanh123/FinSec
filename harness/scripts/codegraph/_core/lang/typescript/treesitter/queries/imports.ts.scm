(import_require_clause source: (string) @import_eq.source) @import_eq.clause
(export_statement "=" (_) @export_eq.value) @export_eq.stmt
(ambient_declaration (module name: (string) @ambient_module.name)) @ambient_module.decl
; host keeps only those before the first non-comment statement
((comment) @triple_slash (#match? @triple_slash "^///[ \t]*<reference[ \t]"))
