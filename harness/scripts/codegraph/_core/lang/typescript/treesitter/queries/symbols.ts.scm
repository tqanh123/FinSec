(program [
  (abstract_class_declaration name: (_) @name) @def.class
  (interface_declaration name: (_) @name) @def.interface
  (type_alias_declaration name: (_) @name) @def.type
  (enum_declaration name: (_) @name) @def.enum
  (function_signature name: (identifier) @name) @def.overload
  (expression_statement (internal_module name: (_) @name) @def.namespace)
  (export_statement declaration: [
    (abstract_class_declaration name: (_) @name) @def.class
    (interface_declaration name: (_) @name) @def.interface
    (type_alias_declaration name: (_) @name) @def.type
    (enum_declaration name: (_) @name) @def.enum
    (function_signature name: (identifier) @name) @def.overload
    (internal_module name: (_) @name) @def.namespace
    (ambient_declaration [
      (function_signature name: (identifier) @name) @def.function
      (class_declaration name: (_) @name) @def.class
      (lexical_declaration (variable_declarator name: (identifier) @name) @def.variable)
    ])
  ])
])
(class_body (abstract_method_signature name: (_) @method.name) @method.def)
(class_heritage (extends_clause value: (_) @heritage.extends))
(class_heritage (implements_clause (_) @heritage.implements))
