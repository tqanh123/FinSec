(program [
  (function_declaration name: (identifier) @name) @def.function
  (generator_function_declaration name: (identifier) @name) @def.function
  (class_declaration name: (_) @name) @def.class
  (lexical_declaration (variable_declarator name: (identifier) @name) @def.variable)
  (variable_declaration (variable_declarator name: (identifier) @name) @def.variable)
  (export_statement declaration: [
    (function_declaration name: (identifier) @name) @def.function
    (generator_function_declaration name: (identifier) @name) @def.function
    (class_declaration name: (_) @name) @def.class
    (lexical_declaration (variable_declarator name: (identifier) @name) @def.variable)
    (variable_declaration (variable_declarator name: (identifier) @name) @def.variable)
  ])
])
(program (export_statement "default" value: (_) @default.value) @default.stmt)
(class_body (method_definition name: (_) @method.name) @method.def)
; CommonJS: module.exports = ... / module.exports.x = ... / exports.x = ...  (target text checked in host)
(program (expression_statement (assignment_expression
  left: (member_expression) @cjs.target
  right: (_) @cjs.value)) @cjs.stmt)
