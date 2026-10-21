; static import (incl. side-effect, type-only: decode clause + "type" token in host code)
(import_statement source: (string) @import.source) @import.stmt
; re-exports: export * from / export * as ns from / export {a, b as c} from / export type {T} from
(export_statement source: (string) @reexport.source) @reexport.stmt
; dynamic import(): host takes the first NON-comment named arg (webpack magic comments), and
; classifies it as dynamic_type when an ancestor is a type context (typeof import(), import("m").T)
(call_expression
  function: (import)
  arguments: (arguments)) @dynamic.call
; CommonJS require(), bare identifier callee only (not obj.require)
(call_expression
  function: (identifier) @_req
  arguments: (arguments . (_) @require.arg)
  (#eq? @_req "require")) @require.call
; local export list: export { a, b as c, d as default }   (no `from`)
(export_statement !source (export_clause) @export_list.clause) @export_list.stmt
; CommonJS exports: module.exports = ... / module.exports.x = ... / exports.x = ...
(program (expression_statement (assignment_expression
  left: (member_expression) @cjs_export.target
  (#match? @cjs_export.target "^(module\\.exports|exports)(\\.|$)")) @cjs_export.assign))
