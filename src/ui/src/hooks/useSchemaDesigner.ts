import { useState, useCallback, useEffect } from 'react';
import { produce } from 'immer';
import {
  X_AWS_IDP_DOCUMENT_TYPE,
  X_AWS_IDP_POLICY_TYPE,
  X_AWS_IDP_EXAMPLES,
  X_AWS_IDP_DOCUMENT_NAME_REGEX,
  X_AWS_IDP_PAGE_CONTENT_REGEX,
  X_AWS_IDP_EXTRACTION_MODEL,
  X_AWS_IDP_EXTRACTION_ESCALATION_MODEL,
  X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT,
  X_AWS_IDP_EXTRACTION_TASK_PROMPT,
  X_AWS_IDP_EXCLUDE_FROM_PROCESSING,
  X_AWS_IDP_EXCLUSION_REASON,
  X_AWS_IDP_INSTANCE_ARRAY,
  X_AWS_IDP_MULTI_INSTANCE,
  X_AWS_IDP_ALLOW_INTEGRATED_LISTS,
  X_AWS_IDP_EVALUATION_MATCH_THRESHOLD,
  SUBSCHEMA_KEYWORDS,
  SUBSCHEMA_MAP_KEYWORDS,
  DESIGNER_ONLY_KEYS,
  SELF_DESCRIBING_KEYWORDS,
  INLINE_OBJECT_KEYWORDS,
  TYPE_OBJECT,
} from '../constants/schemaConstants';
import { refNode } from '../components/json-schema-builder/utils/schemaHelpers';

interface JsonSchemaProperty {
  type?: string;
  description?: string;
  properties?: Record<string, JsonSchemaProperty>;
  required?: string[];
  items?: JsonSchemaProperty;
  $ref?: string;
  $defs?: Record<string, JsonSchemaProperty>;
  $schema?: string;
  $id?: string;
  enum?: unknown[];
  const?: unknown;
  [key: string]: unknown;
}

interface SchemaClass {
  id: string;
  name: string;
  description?: string;
  attributes: {
    // Optional: a definition that describes its own shape — an alias or an enumeration —
    // declares no type, and inventing one for it publishes a contradiction.
    type?: string;
    properties: Record<string, JsonSchemaProperty>;
    required: string[];
    // A `$defs` entry does not have to be an object, and the keywords that describe a
    // scalar or enumerated one — `enum`, `pattern`, `format`, `minimum`, … — have no
    // dedicated slot. They ride here so the definition survives a load-and-save.
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

interface SchemaAttribute {
  id: string;
  name: string;
  type: string;
  description: string;
  properties?: Record<string, JsonSchemaProperty>;
  required?: string[];
  items?: JsonSchemaProperty;
  $ref?: string;
  [key: string]: unknown;
}

interface UseSchemaDesignerReturn {
  classes: SchemaClass[];
  selectedClassId: string | null;
  setSelectedClassId: React.Dispatch<React.SetStateAction<string | null>>;
  selectedAttributeId: string | null;
  setSelectedAttributeId: React.Dispatch<React.SetStateAction<string | null>>;
  isDirty: boolean;
  addClass: (name: string, description?: string) => SchemaClass;
  addStandardClasses: (schemas: JsonSchemaProperty[]) => void;
  updateClass: (classId: string, updates: Record<string, unknown>) => void;
  removeClass: (classId: string) => void;
  addAttribute: (classId: string, attributeName: string, attributeType: string) => SchemaAttribute;
  updateAttribute: (classId: string, attributeName: string, updates: Record<string, unknown>) => void;
  renameAttribute: (classId: string, oldName: string, newName: string) => boolean;
  removeAttribute: (classId: string, attributeName: string) => void;
  reorderAttributes: (classId: string, oldIndex: number, newIndex: number) => void;
  exportSchema: (classNameFilter?: string[]) => JsonSchemaProperty[] | null;
  importSchema: (importedClasses: SchemaClass[]) => void;
  resetDirty: () => void;
  getSelectedClass: () => SchemaClass | undefined;
  getSelectedAttribute: () => JsonSchemaProperty | undefined;
  clearAllClasses: () => void;
}

/**
 * Extract a human-readable name from a JSON Schema $id field.
 * If $id is a URL (e.g., "https://schema.example.com/BankCheck"), returns the last path segment ("BankCheck").
 * If $id is already a simple name, returns it as-is.
 * Returns undefined if $id is falsy.
 */
const extractNameFromId = (id: string | undefined): string | undefined => {
  if (!id) return undefined;
  // If it looks like a URL, extract the last path segment
  if (id.includes('://') || id.startsWith('/')) {
    const segments = id.split('/').filter(Boolean);
    return segments.length > 0 ? segments[segments.length - 1] : id;
  }
  return id;
};

/**
 * The class body for a `$defs` entry, preserving what the entry says about itself.
 *
 * A definition does not have to be an object: `{"type": "string", "enum": [...]}` is a
 * legal `$defs` entry, and one the backend reads correctly — both `deref_schema` and
 * `bedrock/tool_schema.py` follow a `$ref` and take the target's own `type`. Building
 * every entry as `{type: 'object', properties}` therefore destroyed such a definition
 * the first time its class was opened in the Schema Builder and saved: its `type`, its
 * `enum` and any constraint keywords were gone, and it was rewritten as an empty object.
 */
const defClassBody = (defSchema: JsonSchemaProperty, properties: Record<string, JsonSchemaProperty>): SchemaClass['attributes'] => {
  const { type, properties: _properties, required, description: _description, ...constraints } = defSchema;
  // No type is invented for a definition that already describes its own shape — an alias
  // or an enumeration. Defaulting here rather than at export time is still a default: the
  // body would reach `exportSchema` already carrying `type: 'object'`, and the pointer or
  // the enum would be published beside a type contradicting it.
  const describesItself = SELF_DESCRIBING_KEYWORDS.some((keyword) => constraints[keyword] !== undefined);
  return {
    ...(type ? { type } : describesItself ? {} : { type: TYPE_OBJECT }),
    ...constraints,
    properties,
    required: required || [],
  };
};

const extractInlineObjectsToClasses = (
  properties: Record<string, JsonSchemaProperty>,
  extractedClasses: Map<string, SchemaClass>,
  timestamp: number,
): Record<string, JsonSchemaProperty> => {
  const updatedProperties: Record<string, JsonSchemaProperty> = {};

  Object.entries(properties).forEach(([propName, propSchema]) => {
    // Check if this is an inline object with properties (not a $ref)
    if (propSchema.type === 'object' && propSchema.properties && Object.keys(propSchema.properties).length > 0 && !propSchema.$ref) {
      // Extract to a shared class
      const className = propName;
      const classId = `class-${timestamp}-extracted-${className}`;

      // Recursively extract nested objects from this object's properties
      const nestedProperties = extractInlineObjectsToClasses(propSchema.properties, extractedClasses, timestamp);

      extractedClasses.set(className, {
        id: classId,
        name: className,
        description: propSchema.description,
        [X_AWS_IDP_DOCUMENT_TYPE]: false,
        attributes: {
          type: 'object',
          properties: nestedProperties,
          required: propSchema.required || [],
        },
      });

      // Replace the inline object with a reference to the class just extracted.
      // `type`, `properties` and `required` all described the inline object and now
      // live on the `$defs` entry, so the referencing node keeps none of them — the
      // same shape both reference-picking routes in the UI write (#957).
      const { type: _type, properties: _props, required: _required, ...otherProps } = propSchema;
      updatedProperties[propName] = {
        ...otherProps,
        ...refNode(className),
      };
    } else if (propSchema.type === 'array' && propSchema.items) {
      // Check if array items are inline objects
      if (
        propSchema.items.type === 'object' &&
        propSchema.items.properties &&
        Object.keys(propSchema.items.properties).length > 0 &&
        !propSchema.items.$ref
      ) {
        // Extract array item object to a shared class
        const className = propName.endsWith('s') ? propName.slice(0, -1) : `${propName}Item`;
        const classId = `class-${timestamp}-extracted-${className}`;

        // Recursively extract nested objects
        const nestedProperties = extractInlineObjectsToClasses(propSchema.items.properties, extractedClasses, timestamp);

        extractedClasses.set(className, {
          id: classId,
          name: className,
          description: propSchema.items.description,
          [X_AWS_IDP_DOCUMENT_TYPE]: false,
          attributes: {
            type: 'object',
            properties: nestedProperties,
            required: propSchema.items.required || [],
          },
        });

        // Replace inline object with $ref
        updatedProperties[propName] = {
          ...propSchema,
          items: refNode(className),
        };
      } else {
        updatedProperties[propName] = propSchema;
      }
    } else {
      updatedProperties[propName] = propSchema;
    }
  });

  return updatedProperties;
};

const convertJsonSchemaToClasses = (jsonSchema: JsonSchemaProperty | JsonSchemaProperty[]): SchemaClass[] => {
  if (!jsonSchema) return [];

  // Handle array input
  if (Array.isArray(jsonSchema)) {
    // Check if it's already in class array format (has 'attributes' property)
    if (jsonSchema.length > 0 && jsonSchema[0].attributes) {
      return jsonSchema.map((cls) => {
        const schemaClass = cls as unknown as SchemaClass;
        if (!schemaClass.id) {
          return {
            ...schemaClass,
            id: `class-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
          };
        }
        return schemaClass;
      });
    }

    // Handle array of JSON schemas (multi-document-type format)
    const allClasses: SchemaClass[] = [];
    const processedDefs = new Map<string, SchemaClass>();
    const extractedClasses = new Map<string, SchemaClass>();
    const timestamp = Date.now();

    // First pass: collect all document type names
    const docTypeNames = new Set<string>();
    jsonSchema.forEach((schema: JsonSchemaProperty) => {
      const docTypeName = (schema.$id || schema[X_AWS_IDP_DOCUMENT_TYPE] || null) as string | null;
      if (docTypeName) {
        docTypeNames.add(docTypeName);
      }
    });

    jsonSchema.forEach((schema: JsonSchemaProperty, schemaIndex: number) => {
      // Extract inline objects to classes before creating document type
      // Handle both standard 'properties' and 'rule_properties' fields
      const schemaProperties = (schema.rule_properties as Record<string, JsonSchemaProperty> | undefined) || schema.properties || {};
      const extractedProperties = extractInlineObjectsToClasses(schemaProperties, extractedClasses, timestamp);

      // Convert root schema to document type class
      const docTypeClass: SchemaClass = {
        id: `class-${timestamp}-doc-${schemaIndex}`,
        name:
          (schema.title as string) ||
          extractNameFromId(schema.$id as string) ||
          (schema[X_AWS_IDP_DOCUMENT_TYPE] as string) ||
          `DocumentType${schemaIndex + 1}`,
        description: schema.description,
        [X_AWS_IDP_DOCUMENT_TYPE]: true,
        attributes: {
          type: 'object',
          properties: extractedProperties,
          required: schema.required || [],
        },
        // Preserve examples if they exist in the schema
        ...(schema[X_AWS_IDP_EXAMPLES] ? { [X_AWS_IDP_EXAMPLES]: schema[X_AWS_IDP_EXAMPLES] } : {}),
        // Preserve regex fields if they exist in the schema
        ...(schema[X_AWS_IDP_DOCUMENT_NAME_REGEX] ? { [X_AWS_IDP_DOCUMENT_NAME_REGEX]: schema[X_AWS_IDP_DOCUMENT_NAME_REGEX] } : {}),
        ...(schema[X_AWS_IDP_PAGE_CONTENT_REGEX] ? { [X_AWS_IDP_PAGE_CONTENT_REGEX]: schema[X_AWS_IDP_PAGE_CONTENT_REGEX] } : {}),
        // Preserve extraction model override if it exists in the schema
        ...(schema[X_AWS_IDP_EXTRACTION_MODEL] ? { [X_AWS_IDP_EXTRACTION_MODEL]: schema[X_AWS_IDP_EXTRACTION_MODEL] } : {}),
        // Preserve escalation model override if it exists in the schema
        ...(schema[X_AWS_IDP_EXTRACTION_ESCALATION_MODEL]
          ? { [X_AWS_IDP_EXTRACTION_ESCALATION_MODEL]: schema[X_AWS_IDP_EXTRACTION_ESCALATION_MODEL] }
          : {}),
        // Preserve extraction prompt overrides if they exist in the schema
        ...(schema[X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT]
          ? { [X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT]: schema[X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT] }
          : {}),
        ...(schema[X_AWS_IDP_EXTRACTION_TASK_PROMPT]
          ? { [X_AWS_IDP_EXTRACTION_TASK_PROMPT]: schema[X_AWS_IDP_EXTRACTION_TASK_PROMPT] }
          : {}),
        // Preserve excluded-class flags if they exist in the schema
        ...(schema[X_AWS_IDP_EXCLUDE_FROM_PROCESSING]
          ? { [X_AWS_IDP_EXCLUDE_FROM_PROCESSING]: schema[X_AWS_IDP_EXCLUDE_FROM_PROCESSING] }
          : {}),
        ...(schema[X_AWS_IDP_EXCLUSION_REASON] ? { [X_AWS_IDP_EXCLUSION_REASON]: schema[X_AWS_IDP_EXCLUSION_REASON] } : {}),
        ...(schema[X_AWS_IDP_INSTANCE_ARRAY] ? { [X_AWS_IDP_INSTANCE_ARRAY]: schema[X_AWS_IDP_INSTANCE_ARRAY] } : {}),
        ...(schema[X_AWS_IDP_MULTI_INSTANCE] ? { [X_AWS_IDP_MULTI_INSTANCE]: schema[X_AWS_IDP_MULTI_INSTANCE] } : {}),
        ...(schema[X_AWS_IDP_ALLOW_INTEGRATED_LISTS]
          ? { [X_AWS_IDP_ALLOW_INTEGRATED_LISTS]: schema[X_AWS_IDP_ALLOW_INTEGRATED_LISTS] }
          : {}),
        ...(schema[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] !== undefined
          ? { [X_AWS_IDP_EVALUATION_MATCH_THRESHOLD]: schema[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] }
          : {}),
      };
      allClasses.push(docTypeClass);

      // Process $defs (non-document-type classes)
      if (schema.$defs) {
        Object.entries(schema.$defs).forEach(([defName, defSchema]: [string, JsonSchemaProperty]) => {
          // Skip if this def is already a document type (prevents duplicates)
          if (docTypeNames.has(defName)) {
            console.log(`Skipping $def "${defName}" because it's already imported as a document type`);
            return;
          }

          if (!processedDefs.has(defName)) {
            // Extract inline objects from $def properties
            const extractedDefProperties = extractInlineObjectsToClasses(defSchema.properties || {}, extractedClasses, timestamp);

            const defClass: SchemaClass = {
              id: `class-${timestamp}-def-${defName}`,
              name: defName,
              description: defSchema.description,
              [X_AWS_IDP_DOCUMENT_TYPE]: false,
              attributes: defClassBody(defSchema, extractedDefProperties),
            };
            processedDefs.set(defName, defClass);
          }
        });
      }
    });

    // Add extracted inline object classes first (so they're available for references)
    extractedClasses.forEach((cls) => allClasses.push(cls));

    // Add all unique $defs classes
    processedDefs.forEach((cls) => allClasses.push(cls));

    return allClasses;
  }

  // Handle single JSON schema (legacy format)
  const classes: SchemaClass[] = [];
  const extractedClasses = new Map<string, SchemaClass>();
  const timestamp = Date.now();

  // Extract inline objects from main schema
  // Handle both standard 'properties' and 'rule_properties' fields
  const schemaProperties = (jsonSchema.rule_properties as Record<string, JsonSchemaProperty> | undefined) || jsonSchema.properties || {};
  const extractedProperties = extractInlineObjectsToClasses(schemaProperties, extractedClasses, timestamp);

  const mainClassId = `class-${timestamp}`;
  const mainClass: SchemaClass = {
    id: mainClassId,
    name: (jsonSchema.title as string) || extractNameFromId(jsonSchema.$id as string) || 'MainClass',
    description: jsonSchema.description,
    [X_AWS_IDP_DOCUMENT_TYPE]: true, // Mark as document type for backward compat
    attributes: {
      type: 'object',
      properties: extractedProperties,
      required: jsonSchema.required || [],
    },
    // Preserve examples if they exist in the schema
    ...(jsonSchema[X_AWS_IDP_EXAMPLES] ? { [X_AWS_IDP_EXAMPLES]: jsonSchema[X_AWS_IDP_EXAMPLES] } : {}),
    // Preserve regex fields if they exist in the schema
    ...(jsonSchema[X_AWS_IDP_DOCUMENT_NAME_REGEX] ? { [X_AWS_IDP_DOCUMENT_NAME_REGEX]: jsonSchema[X_AWS_IDP_DOCUMENT_NAME_REGEX] } : {}),
    ...(jsonSchema[X_AWS_IDP_PAGE_CONTENT_REGEX] ? { [X_AWS_IDP_PAGE_CONTENT_REGEX]: jsonSchema[X_AWS_IDP_PAGE_CONTENT_REGEX] } : {}),
    // Preserve extraction model override if it exists in the schema
    ...(jsonSchema[X_AWS_IDP_EXTRACTION_MODEL] ? { [X_AWS_IDP_EXTRACTION_MODEL]: jsonSchema[X_AWS_IDP_EXTRACTION_MODEL] } : {}),
    // Preserve escalation model override if it exists in the schema
    ...(jsonSchema[X_AWS_IDP_EXTRACTION_ESCALATION_MODEL]
      ? { [X_AWS_IDP_EXTRACTION_ESCALATION_MODEL]: jsonSchema[X_AWS_IDP_EXTRACTION_ESCALATION_MODEL] }
      : {}),
    // Preserve extraction prompt overrides if they exist in the schema
    ...(jsonSchema[X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT]
      ? { [X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT]: jsonSchema[X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT] }
      : {}),
    ...(jsonSchema[X_AWS_IDP_EXTRACTION_TASK_PROMPT]
      ? { [X_AWS_IDP_EXTRACTION_TASK_PROMPT]: jsonSchema[X_AWS_IDP_EXTRACTION_TASK_PROMPT] }
      : {}),
    // Preserve excluded-class flags if they exist in the schema
    ...(jsonSchema[X_AWS_IDP_EXCLUDE_FROM_PROCESSING]
      ? { [X_AWS_IDP_EXCLUDE_FROM_PROCESSING]: jsonSchema[X_AWS_IDP_EXCLUDE_FROM_PROCESSING] }
      : {}),
    ...(jsonSchema[X_AWS_IDP_EXCLUSION_REASON] ? { [X_AWS_IDP_EXCLUSION_REASON]: jsonSchema[X_AWS_IDP_EXCLUSION_REASON] } : {}),
    ...(jsonSchema[X_AWS_IDP_INSTANCE_ARRAY] ? { [X_AWS_IDP_INSTANCE_ARRAY]: jsonSchema[X_AWS_IDP_INSTANCE_ARRAY] } : {}),
    ...(jsonSchema[X_AWS_IDP_MULTI_INSTANCE] ? { [X_AWS_IDP_MULTI_INSTANCE]: jsonSchema[X_AWS_IDP_MULTI_INSTANCE] } : {}),
    ...(jsonSchema[X_AWS_IDP_ALLOW_INTEGRATED_LISTS]
      ? { [X_AWS_IDP_ALLOW_INTEGRATED_LISTS]: jsonSchema[X_AWS_IDP_ALLOW_INTEGRATED_LISTS] }
      : {}),
    ...(jsonSchema[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] !== undefined
      ? { [X_AWS_IDP_EVALUATION_MATCH_THRESHOLD]: jsonSchema[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] }
      : {}),
  };
  classes.push(mainClass);

  if (jsonSchema.$defs) {
    let defIndex = 0;
    Object.entries(jsonSchema.$defs).forEach(([defName, defSchema]: [string, JsonSchemaProperty]) => {
      defIndex += 1;

      // Extract inline objects from $def properties
      const extractedDefProperties = extractInlineObjectsToClasses(defSchema.properties || {}, extractedClasses, timestamp);

      classes.push({
        id: `class-${timestamp}-def-${defIndex}`,
        name: defName,
        description: defSchema.description,
        [X_AWS_IDP_DOCUMENT_TYPE]: false, // Shared class, not a document type
        attributes: defClassBody(defSchema, extractedDefProperties),
      });
    });
  }

  // Add extracted inline object classes
  extractedClasses.forEach((cls) => classes.push(cls));

  return classes;
};

export const useSchemaDesigner = (
  initialSchema: JsonSchemaProperty | JsonSchemaProperty[] = [],
  isRuleSchema: boolean = false,
): UseSchemaDesignerReturn => {
  const [classes, setClasses] = useState<SchemaClass[]>([]);
  const [selectedClassId, setSelectedClassId] = useState<string | null>(null);
  const [selectedAttributeId, setSelectedAttributeId] = useState<string | null>(null);
  const [isDirty, setIsDirty] = useState(false);
  const [initialized, setInitialized] = useState(false);

  useEffect(() => {
    if (!initialized && initialSchema) {
      const newClasses = convertJsonSchemaToClasses(initialSchema);
      if (newClasses.length > 0) {
        setClasses(newClasses);
        setSelectedClassId(newClasses[0].id);
        setInitialized(true);
      }
    }
  }, [initialSchema, initialized]);

  const addClass = useCallback((name: string, description?: string): SchemaClass => {
    const newClass: SchemaClass = {
      id: `class-${Date.now()}`,
      name,
      ...(description ? { description } : {}),
      attributes: {
        type: 'object',
        properties: {},
        required: [],
      },
    };
    setClasses((prev) => [...prev, newClass]);
    setSelectedClassId(newClass.id);
    setIsDirty(true);
    return newClass;
  }, []);

  const addStandardClasses = useCallback((schemas: JsonSchemaProperty[]) => {
    // Convert the standard JSON schemas to internal SchemaClass format
    // This reuses the same convertJsonSchemaToClasses logic used for import
    const newClasses = convertJsonSchemaToClasses(schemas);
    if (newClasses.length > 0) {
      setClasses((prev) => [...prev, ...newClasses]);
      setSelectedClassId(newClasses[0].id);
      setSelectedAttributeId(null);
      setIsDirty(true);
    }
  }, []);

  const updateClass = useCallback((classId: string, updates: Record<string, unknown>) => {
    setClasses((prev) =>
      produce(prev, (draft) => {
        const cls = draft.find((c) => c.id === classId);
        if (cls) {
          // Deep merge updates to ensure immer properly tracks all changes
          // This prevents mixing external references with draft objects
          Object.keys(updates).forEach((key) => {
            if (key === 'attributes' && typeof updates[key] === 'object' && updates[key] !== null) {
              // Handle nested attributes object specially
              if (!cls.attributes) {
                cls.attributes = { type: 'object', properties: {}, required: [] };
              }
              const updatesAttrs = updates.attributes as Record<string, unknown>;
              Object.keys(updatesAttrs).forEach((attrKey) => {
                (cls.attributes as Record<string, unknown>)[attrKey] = updatesAttrs[attrKey];
              });
            } else {
              // Direct assignment for top-level properties
              cls[key] = updates[key];
            }
          });
        }
      }),
    );
    setIsDirty(true);
  }, []);

  const removeClass = useCallback(
    (classId: string) => {
      setClasses((prev) => prev.filter((cls) => cls.id !== classId));
      if (selectedClassId === classId) {
        setSelectedClassId(null);
      }
      setIsDirty(true);
    },
    [selectedClassId],
  );

  const addAttribute = useCallback((classId: string, attributeName: string, attributeType: string): SchemaAttribute => {
    const newAttribute: SchemaAttribute = {
      id: `attr-${Date.now()}`,
      name: attributeName,
      type: attributeType,
      description: '',
    };

    if (attributeType === 'object') {
      newAttribute.properties = {};
      newAttribute.required = [];
    }

    if (attributeType === 'array') {
      newAttribute.items = {
        id: `item-${Date.now()}`,
        name: 'item',
        type: 'string',
        description: '',
      };
    }

    setClasses((prev) =>
      produce(prev, (draft) => {
        const cls = draft.find((c) => c.id === classId);
        if (cls) {
          // Giving a scalar or enumerated definition a property converts it to an object.
          // The alternative is a body that says `type: 'string'` and carries `properties`,
          // or an `enum` beside them — which is the corruption the round-trip fix exists to
          // prevent, arrived at from the editor instead of the importer. The keywords that
          // described the old scalar go with the type that declared them.
          if (cls.attributes.type && cls.attributes.type !== TYPE_OBJECT) {
            SELF_DESCRIBING_KEYWORDS.forEach((keyword) => delete cls.attributes[keyword]);
            delete cls.attributes.pattern;
            delete cls.attributes.format;
            cls.attributes.type = TYPE_OBJECT;
          }
          cls.attributes.properties[attributeName] = newAttribute;
        }
      }),
    );
    setIsDirty(true);
    return newAttribute;
  }, []);

  const updateAttribute = useCallback((classId: string, attributeName: string, updates: Record<string, unknown>) => {
    setClasses((prev) =>
      produce(prev, (draft) => {
        const cls = draft.find((c) => c.id === classId);
        if (cls && cls.attributes.properties[attributeName]) {
          const attr = cls.attributes.properties[attributeName];
          if (typeof updates === 'object' && Object.keys(updates).length > 0) {
            // Apply updates, deleting keys with undefined values
            Object.keys(updates).forEach((key) => {
              if (updates[key] === undefined) {
                delete attr[key];
              } else {
                attr[key] = updates[key];
              }
            });
          } else {
            // Merge updates
            Object.assign(attr, updates);
          }
        }
      }),
    );
    setIsDirty(true);
  }, []);

  const renameAttribute = useCallback(
    (classId: string, oldName: string, newName: string): boolean => {
      const trimmedName = newName.trim();
      if (!trimmedName || trimmedName === oldName) {
        return false;
      }

      let renameSuccessful = false;

      setClasses((prev) =>
        produce(prev, (draft) => {
          const cls = draft.find((c) => c.id === classId);
          if (!cls || !cls.attributes.properties[oldName] || cls.attributes.properties[trimmedName]) {
            return;
          }

          // Rename the attribute
          const attribute = cls.attributes.properties[oldName];
          attribute.name = trimmedName;
          cls.attributes.properties[trimmedName] = attribute;
          delete cls.attributes.properties[oldName];

          // Update required array
          if (cls.attributes.required) {
            const index = cls.attributes.required.indexOf(oldName);
            if (index !== -1) {
              cls.attributes.required[index] = trimmedName;
            }
          }

          renameSuccessful = true;
        }),
      );

      if (renameSuccessful) {
        setSelectedAttributeId((prev) => (prev === oldName ? trimmedName : prev));
        setIsDirty(true);
      }

      return renameSuccessful;
    },
    [setSelectedAttributeId],
  );

  const removeAttribute = useCallback(
    (classId: string, attributeName: string) => {
      setClasses((prev) =>
        produce(prev, (draft) => {
          const cls = draft.find((c) => c.id === classId);
          if (cls) {
            delete cls.attributes.properties[attributeName];
            if (cls.attributes.required) {
              cls.attributes.required = cls.attributes.required.filter((name) => name !== attributeName);
            }
          }
        }),
      );
      if (selectedAttributeId === attributeName) {
        setSelectedAttributeId(null);
      }
      setIsDirty(true);
    },
    [selectedAttributeId],
  );

  const reorderAttributes = useCallback((classId: string, oldIndex: number, newIndex: number) => {
    setClasses((prev) =>
      produce(prev, (draft) => {
        const cls = draft.find((c) => c.id === classId);
        if (cls) {
          const entries = Object.entries(cls.attributes.properties);
          const [removed] = entries.splice(oldIndex, 1);
          entries.splice(newIndex, 0, removed);
          cls.attributes.properties = Object.fromEntries(entries);
        }
      }),
    );
    setIsDirty(true);
  }, []);

  const sanitizeAttributeSchema = useCallback((attribute: unknown): JsonSchemaProperty => {
    // `oneOf` and friends hold a list of subschemas, so the walk has to descend into
    // arrays as well as objects.
    if (Array.isArray(attribute)) {
      return attribute.map((entry) => sanitizeAttributeSchema(entry)) as unknown as JsonSchemaProperty;
    }

    if (!attribute || typeof attribute !== 'object') {
      return attribute as JsonSchemaProperty;
    }

    const attrObj = attribute as JsonSchemaProperty;
    const sanitized: JsonSchemaProperty = { ...attrObj };
    DESIGNER_ONLY_KEYS.forEach((key) => delete sanitized[key]);

    // A `$ref` delegates the whole type designation to the referenced `$defs` entry, so
    // every keyword describing an inline object goes with it. The same list
    // `refAttributeUpdates` clears when it writes a reference, so a node normalizes to
    // the shape that helper would have produced however it acquired its siblings.
    if (sanitized.$ref) {
      INLINE_OBJECT_KEYWORDS.forEach((keyword) => delete sanitized[keyword]);
    }

    // Recursing into `items` and `properties` alone left every composition, conditional
    // and `contains` branch unsanitized, so a `$ref`-beside-`type` one level down went out
    // as written — and a designer key in a `$defs` body went out with it.
    SUBSCHEMA_KEYWORDS.forEach((keyword) => {
      const value = sanitized[keyword];
      if (value && typeof value === 'object') {
        sanitized[keyword] = sanitizeAttributeSchema(value);
      }
    });

    SUBSCHEMA_MAP_KEYWORDS.forEach((keyword) => {
      const map = sanitized[keyword];
      if (!map || typeof map !== 'object') return;
      sanitized[keyword] = Object.entries(map as Record<string, JsonSchemaProperty>).reduce(
        (acc: Record<string, JsonSchemaProperty>, [propName, propValue]) => {
          acc[propName] = sanitizeAttributeSchema(propValue);
          return acc;
        },
        {},
      );
    });

    return sanitized;
  }, []);

  // Helper: Find all classes referenced by a class (recursively)
  const findReferencedClasses = useCallback(
    (rootClass: SchemaClass, visited: Set<string> = new Set()): SchemaClass[] => {
      console.log(`  findReferencedClasses for: ${rootClass.name}`);
      const referenced: SchemaClass[] = [];

      const addRef = (ref: unknown) => {
        if (typeof ref !== 'string' || !ref) return;
        const refName = ref.replace('#/$defs/', '');
        if (visited.has(refName)) return;

        const refClass = classes.find((c) => c.name === refName);
        if (!refClass) {
          console.log(
            `      ❌ No class found with name "${refName}". Available classes:`, // nosemgrep: javascript.lang.security.audit.unsafe-formatstring.unsafe-formatstring - Controlled input from schema validation, not user input
            classes.map((c) => c.name),
          );
          return;
        }

        visited.add(refName);
        referenced.push(refClass);
        // Recursively find references in this class
        referenced.push(...findReferencedClasses(refClass, visited));
      };

      /**
       * Every `$ref` reachable from one node, wherever it sits: on the node itself, on
       * an array's `items`, inside a composition or conditional branch, or on a nested
       * inline object's properties.
       *
       * The branches matter because the `contains` builder, and the composition and
       * conditional editors, can each point one at a shared class. A class reached only
       * that way was left out of `$defs`, which publishes a schema whose own pointer
       * resolves to nothing.
       */
      const collectRefs = (node: unknown) => {
        if (Array.isArray(node)) {
          node.forEach(collectRefs);
          return;
        }
        if (!node || typeof node !== 'object') return;

        const obj = node as JsonSchemaProperty;
        addRef(obj.$ref);
        SUBSCHEMA_KEYWORDS.forEach((keyword) => collectRefs(obj[keyword]));
        SUBSCHEMA_MAP_KEYWORDS.forEach((keyword) => {
          const map = obj[keyword];
          if (map && typeof map === 'object') Object.values(map).forEach(collectRefs);
        });
      };

      // The whole class body, not only its properties. A `$defs` entry may be an alias
      // (`{"$ref": …}`) or an array of a shared class (`{"type": "array", "items":
      // {"$ref": …}}`), and a reference sitting there is as much part of the schema as
      // one on a property. Walking only `properties` left the target out of `$defs`.
      collectRefs(rootClass.attributes);
      console.log(`  Total referenced classes found: ${referenced.length}`);
      return referenced;
    },
    [classes],
  );

  // Export the schema as an array of per-document-type JSON Schemas. Pass
  // `classNameFilter` (a list of document-type class names) to export only those
  // document types (each still carries its own referenced $defs); omit it to
  // export every document type — the behavior the live onChange/preview path uses.
  const exportSchema = useCallback(
    (classNameFilter?: string[]): JsonSchemaProperty[] | null => {
      if (classes.length === 0) {
        return null;
      }

      // Find all document type classes
      const docTypeClasses = classes.filter((cls) => cls[X_AWS_IDP_DOCUMENT_TYPE] === true);

      // If no document types, fall back to treating first class as document type (backward compat)
      let baseClasses = docTypeClasses.length > 0 ? docTypeClasses : [classes[0]];

      // Optionally restrict to a caller-selected subset of document types.
      if (classNameFilter && classNameFilter.length > 0) {
        const wanted = new Set(classNameFilter);
        const filtered = baseClasses.filter((cls) => wanted.has(cls.name));
        if (filtered.length > 0) {
          baseClasses = filtered;
        }
      }

      // Build schema for each document type
      const schemas = baseClasses.map((docTypeClass) => {
        // Find classes referenced by this document type
        const referencedClasses = findReferencedClasses(docTypeClass);

        // Build $defs only for referenced classes
        const defs: Record<string, JsonSchemaProperty> = {};
        referencedClasses.forEach((cls) => {
          const sanitizedProps = Object.entries(cls.attributes.properties || {}).reduce(
            (acc: Record<string, JsonSchemaProperty>, [attrName, attrValue]) => {
              acc[attrName] = sanitizeAttributeSchema(attrValue);
              return acc;
            },
            {},
          );

          // What the definition says about itself, not an assumption that it is an
          // object. A scalar or enumerated `$defs` entry keeps its own `type` and
          // constraint keywords, and is written back without an empty `properties` map it
          // never had.
          //
          // Two things this must not do. It must not invent `type: 'object'` for a body
          // that already describes its own shape — an alias would get a `$ref` beside a
          // contradictory `type`, and a typeless `{"enum": [...]}` would get one matching
          // nothing. And it must not hand the body's own keywords through unexamined: they
          // are subschemas like any other, so they go through the sanitizer, which is what
          // keeps a designer key or a nested `$ref`-beside-`type` out of `$defs`.
          const { type: bodyType, properties: _bodyProps, required: bodyRequired, ...bodyConstraints } = cls.attributes;
          const declaredType = typeof bodyType === 'string' && bodyType ? bodyType : null;
          const describesItself = SELF_DESCRIBING_KEYWORDS.some((keyword) => bodyConstraints[keyword] !== undefined);
          const definitionType = declaredType ?? (describesItself ? null : TYPE_OBJECT);
          const carriesProperties = definitionType === TYPE_OBJECT || Object.keys(sanitizedProps).length > 0;

          defs[cls.name] = sanitizeAttributeSchema({
            ...(definitionType ? { type: definitionType } : {}),
            ...(cls.description ? { description: cls.description } : {}),
            ...(bodyConstraints as Record<string, unknown>),
            ...(carriesProperties ? { properties: sanitizedProps } : {}),
            ...(Array.isArray(bodyRequired) && bodyRequired.length > 0 ? { required: bodyRequired } : {}),
          });
        });

        // Build main schema properties
        const sanitizedProps = Object.entries(docTypeClass.attributes.properties || {}).reduce(
          (acc: Record<string, JsonSchemaProperty>, [attrName, attrValue]) => {
            acc[attrName] = sanitizeAttributeSchema(attrValue);
            return acc;
          },
          {},
        );

        // Use conditional field names based on schema type
        const typeField = isRuleSchema ? X_AWS_IDP_POLICY_TYPE : X_AWS_IDP_DOCUMENT_TYPE;
        const propertiesField = isRuleSchema ? 'rule_properties' : 'properties';

        const result: JsonSchemaProperty = {
          $schema: 'https://json-schema.org/draft/2020-12/schema',
          $id: docTypeClass.name,
          [typeField]: docTypeClass.name,
          type: 'object',
          ...(docTypeClass.description ? { description: docTypeClass.description } : {}),
          [propertiesField]: sanitizedProps,
          ...(docTypeClass.attributes.required?.length > 0 ? { required: docTypeClass.attributes.required } : {}),
          ...(Object.keys(defs).length > 0 ? { $defs: defs } : {}),
          ...(Array.isArray(docTypeClass[X_AWS_IDP_EXAMPLES]) && docTypeClass[X_AWS_IDP_EXAMPLES].length > 0
            ? { [X_AWS_IDP_EXAMPLES]: docTypeClass[X_AWS_IDP_EXAMPLES] }
            : {}),
          ...(docTypeClass[X_AWS_IDP_DOCUMENT_NAME_REGEX]
            ? { [X_AWS_IDP_DOCUMENT_NAME_REGEX]: docTypeClass[X_AWS_IDP_DOCUMENT_NAME_REGEX] }
            : {}),
          ...(docTypeClass[X_AWS_IDP_PAGE_CONTENT_REGEX]
            ? { [X_AWS_IDP_PAGE_CONTENT_REGEX]: docTypeClass[X_AWS_IDP_PAGE_CONTENT_REGEX] }
            : {}),
          ...(docTypeClass[X_AWS_IDP_EXTRACTION_MODEL] ? { [X_AWS_IDP_EXTRACTION_MODEL]: docTypeClass[X_AWS_IDP_EXTRACTION_MODEL] } : {}),
          ...(docTypeClass[X_AWS_IDP_EXTRACTION_ESCALATION_MODEL]
            ? { [X_AWS_IDP_EXTRACTION_ESCALATION_MODEL]: docTypeClass[X_AWS_IDP_EXTRACTION_ESCALATION_MODEL] }
            : {}),
          ...(docTypeClass[X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT]
            ? { [X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT]: docTypeClass[X_AWS_IDP_EXTRACTION_SYSTEM_PROMPT] }
            : {}),
          ...(docTypeClass[X_AWS_IDP_EXTRACTION_TASK_PROMPT]
            ? { [X_AWS_IDP_EXTRACTION_TASK_PROMPT]: docTypeClass[X_AWS_IDP_EXTRACTION_TASK_PROMPT] }
            : {}),
          ...(docTypeClass[X_AWS_IDP_EXCLUDE_FROM_PROCESSING]
            ? { [X_AWS_IDP_EXCLUDE_FROM_PROCESSING]: docTypeClass[X_AWS_IDP_EXCLUDE_FROM_PROCESSING] }
            : {}),
          ...(docTypeClass[X_AWS_IDP_EXCLUSION_REASON] ? { [X_AWS_IDP_EXCLUSION_REASON]: docTypeClass[X_AWS_IDP_EXCLUSION_REASON] } : {}),
          ...(docTypeClass[X_AWS_IDP_INSTANCE_ARRAY] ? { [X_AWS_IDP_INSTANCE_ARRAY]: docTypeClass[X_AWS_IDP_INSTANCE_ARRAY] } : {}),
          ...(docTypeClass[X_AWS_IDP_MULTI_INSTANCE] ? { [X_AWS_IDP_MULTI_INSTANCE]: docTypeClass[X_AWS_IDP_MULTI_INSTANCE] } : {}),
          ...(docTypeClass[X_AWS_IDP_ALLOW_INTEGRATED_LISTS]
            ? { [X_AWS_IDP_ALLOW_INTEGRATED_LISTS]: docTypeClass[X_AWS_IDP_ALLOW_INTEGRATED_LISTS] }
            : {}),
          ...(docTypeClass[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] !== undefined
            ? { [X_AWS_IDP_EVALUATION_MATCH_THRESHOLD]: docTypeClass[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] }
            : {}),
        };

        return result;
      });

      // Always return array of schemas for consistency
      return schemas;
    },
    [classes, sanitizeAttributeSchema, findReferencedClasses, isRuleSchema],
  );

  const importSchema = useCallback((importedClasses: SchemaClass[]) => {
    setClasses(importedClasses);
    setSelectedClassId(importedClasses.length > 0 ? importedClasses[0].id : null);
    setSelectedAttributeId(null);
    setIsDirty(false);
  }, []);

  const resetDirty = useCallback(() => {
    setIsDirty(false);
  }, []);

  const getSelectedClass = useCallback((): SchemaClass | undefined => {
    return classes.find((cls) => cls.id === selectedClassId);
  }, [classes, selectedClassId]);

  const getSelectedAttribute = useCallback((): JsonSchemaProperty | undefined => {
    const cls = getSelectedClass();
    if (!selectedAttributeId) return undefined;
    return cls?.attributes?.properties?.[selectedAttributeId];
  }, [getSelectedClass, selectedAttributeId]);

  const clearAllClasses = useCallback(() => {
    setClasses([]);
    setSelectedClassId(null);
    setSelectedAttributeId(null);
    setIsDirty(true);
  }, []);

  return {
    classes,
    selectedClassId,
    setSelectedClassId,
    selectedAttributeId,
    setSelectedAttributeId,
    isDirty,
    addClass,
    addStandardClasses,
    updateClass,
    removeClass,
    addAttribute,
    updateAttribute,
    renameAttribute,
    removeAttribute,
    reorderAttributes,
    exportSchema,
    importSchema,
    resetDirty,
    getSelectedClass,
    getSelectedAttribute,
    clearAllClasses,
  };
};
