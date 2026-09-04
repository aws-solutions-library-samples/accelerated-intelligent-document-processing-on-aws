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
} from '../constants/schemaConstants';

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
    type: string;
    properties: Record<string, JsonSchemaProperty>;
    required: string[];
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

      // Replace inline object with $ref
      // Keep type: 'object' for UI purposes, but remove properties and required
      const { properties: _props, required: _required, ...otherProps } = propSchema;
      updatedProperties[propName] = {
        ...otherProps,
        $ref: `#/$defs/${className}`,
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
          items: {
            $ref: `#/$defs/${className}`,
          },
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
              attributes: {
                type: 'object',
                properties: extractedDefProperties,
                required: defSchema.required || [],
              },
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
        attributes: {
          type: 'object',
          properties: extractedDefProperties,
          required: defSchema.required || [],
        },
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
    if (!attribute || typeof attribute !== 'object') {
      return attribute as JsonSchemaProperty;
    }

    const attrObj = attribute as JsonSchemaProperty;
    const { id: _id, name: _name, ...rest } = attrObj;
    const sanitized: JsonSchemaProperty = { ...rest };

    // CRITICAL FIX: Remove 'type' when '$ref' is present (invalid JSON Schema)
    // When a $ref is used, no other schema keywords (type, properties, etc.) should be present
    if (sanitized.$ref) {
      delete sanitized.type;
      delete sanitized.properties;
      delete sanitized.required;
    }

    if (sanitized.items) {
      sanitized.items = sanitizeAttributeSchema(sanitized.items);
    }

    if (sanitized.properties) {
      const sanitizedProperties = Object.entries(sanitized.properties).reduce(
        (acc: Record<string, JsonSchemaProperty>, [propName, propValue]) => {
          acc[propName] = sanitizeAttributeSchema(propValue);
          return acc;
        },
        {},
      );
      sanitized.properties = sanitizedProperties;
    }

    return sanitized;
  }, []);

  // Helper: Find all classes referenced by a class (recursively)
  const findReferencedClasses = useCallback(
    (rootClass: SchemaClass, visited: Set<string> = new Set()): SchemaClass[] => {
      console.log(`  findReferencedClasses for: ${rootClass.name}`);
      const referenced: SchemaClass[] = [];

      const processProperties = (properties: Record<string, JsonSchemaProperty>) => {
        Object.entries(properties || {}).forEach(([attrName, attr]) => {
          // Check direct $ref
          if (attr.$ref) {
            const refName = attr.$ref.replace('#/$defs/', '');
            console.log(`    Found $ref in "${attrName}": ${attr.$ref} -> looking for class: "${refName}"`);

            if (!visited.has(refName)) {
              const refClass = classes.find((c) => c.name === refName);
              console.log(`      Class found? ${!!refClass}, isDocType? ${refClass?.[X_AWS_IDP_DOCUMENT_TYPE]}`);

              if (!refClass) {
                console.log(
                  `      ❌ No class found with name "${refName}". Available classes:`, // nosemgrep: javascript.lang.security.audit.unsafe-formatstring.unsafe-formatstring - Controlled input from schema validation, not user input
                  classes.map((c) => c.name),
                );
              } else {
                console.log(`      ✅ Adding "${refName}" to referenced classes (isDocType: ${refClass[X_AWS_IDP_DOCUMENT_TYPE]})`);
                visited.add(refName);
                referenced.push(refClass);
                // Recursively find references in this class
                referenced.push(...findReferencedClasses(refClass, visited));
              }
            } else {
              console.log(`      Already visited "${refName}"`);
            }
          }

          // Check array items $ref
          if (attr.items?.$ref) {
            const refName = attr.items.$ref.replace('#/$defs/', '');
            console.log(`    Found items.$ref in "${attrName}": ${attr.items.$ref} -> looking for class: "${refName}"`);

            if (!visited.has(refName)) {
              const refClass = classes.find((c) => c.name === refName);
              console.log(`      Class found? ${!!refClass}, isDocType? ${refClass?.[X_AWS_IDP_DOCUMENT_TYPE]}`);

              if (!refClass) {
                console.log(
                  `      ❌ No class found with name "${refName}". Available classes:`, // nosemgrep: javascript.lang.security.audit.unsafe-formatstring.unsafe-formatstring - Controlled input from schema validation, not user input
                  classes.map((c) => c.name),
                );
              } else {
                console.log(`      ✅ Adding "${refName}" to referenced classes (isDocType: ${refClass[X_AWS_IDP_DOCUMENT_TYPE]})`);
                visited.add(refName);
                referenced.push(refClass);
                referenced.push(...findReferencedClasses(refClass, visited));
              }
            } else {
              console.log(`      Already visited "${refName}"`);
            }
          }

          // Check nested object properties
          if (attr.type === 'object' && attr.properties) {
            processProperties(attr.properties);
          }
        });
      };

      processProperties(rootClass.attributes.properties);
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

          defs[cls.name] = {
            type: 'object',
            ...(cls.description ? { description: cls.description } : {}),
            properties: sanitizedProps,
            ...(cls.attributes.required?.length > 0 ? { required: cls.attributes.required } : {}),
          };
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
