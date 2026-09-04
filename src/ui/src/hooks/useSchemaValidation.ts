import { useState, useCallback, useMemo } from 'react';
import Ajv from 'ajv';
import addFormats from 'ajv-formats';
import { SchemaValidationError } from '../types/common';
import { X_AWS_IDP_INSTANCE_ARRAY, X_AWS_IDP_MULTI_INSTANCE } from '../constants/schemaConstants';

interface ValidatableAttribute {
  type?: string;
  $ref?: string;
  oneOf?: unknown[];
  anyOf?: unknown[];
  allOf?: unknown[];
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  minItems?: number;
  maxItems?: number;
  minContains?: number;
  maxContains?: number;
  minProperties?: number;
  maxProperties?: number;
  const?: unknown;
  enum?: unknown[];
  readOnly?: boolean;
  writeOnly?: boolean;
  [key: string]: unknown;
}

interface SchemaNode {
  $ref?: string;
  properties?: Record<string, SchemaNode>;
  items?: SchemaNode;
  [key: string]: unknown;
}

interface ValidationResult {
  valid: boolean;
  errors: SchemaValidationError[];
}

interface UseSchemaValidationReturn {
  validateSchema: (schema: unknown) => ValidationResult;
  validateAttribute: (attribute: unknown) => ValidationResult;
  detectCircularReferences: (schema: unknown, visited?: Set<string>, path?: string[]) => SchemaValidationError[];
  validateReferences: (schema: unknown, availableClasses?: Array<{ name: string }>) => SchemaValidationError[];
  validationErrors: SchemaValidationError[];
  clearErrors: () => void;
}

const EXTRACTION_JSON_SCHEMA = {
  type: 'object',
  properties: {
    name: {
      type: 'string',
      description: 'The name of the document class',
    },
    description: {
      type: 'string',
      description: 'Description of the document class',
    },
    attributes: {
      type: 'object',
      description: 'The extraction schema defining attributes to extract',
      properties: {},
      additionalProperties: true,
    },
  },
  required: ['name', 'attributes'],
  additionalProperties: true,
};

/**
 * The `x-aws-idp-*` class-level rules the BACKEND hard-rejects at config load.
 *
 * Ajv only checks standard JSON-Schema keywords, so before this an invalid
 * `x-aws-idp-instance-array` was displayed with red `errorText` in the inspector
 * and then saved anyway — the message said "will be rejected on save" and Save
 * worked. The rejection arrived later, from the server, on a config the user had
 * been told was a problem.
 *
 * A client-side PREFLIGHT, not a second source of truth: `IDPConfig` stays
 * authoritative, and these mirror `validate_instance_array` in
 * `idp_common/config/models.py`. Keep the two in step.
 */
const validateIdpClassExtensions = (schema: unknown): SchemaValidationError[] => {
  if (!schema || typeof schema !== 'object') return [];
  const cls = schema as Record<string, unknown>;
  const errors: SchemaValidationError[] = [];

  // The designer shape nests properties under `attributes`; an exported class has
  // them at the top. Accept both so this works wherever it is called from.
  const attributes = cls.attributes as { properties?: Record<string, unknown> } | undefined;
  const properties = (attributes?.properties ?? (cls.properties as Record<string, unknown>) ?? {}) as Record<string, unknown>;

  const designated = cls[X_AWS_IDP_INSTANCE_ARRAY];
  const raw = cls[X_AWS_IDP_MULTI_INSTANCE];
  const multi = raw === true || (typeof raw === 'string' && ['true', 'yes', '1'].includes(raw.trim().toLowerCase()));

  // The synthesized wrapper legitimately carries BOTH keys. It is produced at
  // runtime and never stored, but rejecting a schema the pipeline itself emits
  // would be a nasty trap for anyone who round-trips one.
  const isWrapper = multi && Object.keys(properties).length === 1 && 'instances' in properties;
  if (isWrapper) return [];

  if (multi && designated !== undefined && designated !== null && designated !== '') {
    errors.push({
      path: `/${X_AWS_IDP_MULTI_INSTANCE}`,
      message:
        'A class cannot set both Multi-instance Sections and Instance Array — they are mutually exclusive. ' +
        'Use Multi-instance when the class describes ONE record; use Instance Array when it already lists records.',
    });
  }

  if (multi && 'instances' in properties) {
    errors.push({
      path: `/${X_AWS_IDP_MULTI_INSTANCE}`,
      message:
        'Multi-instance Sections wraps this class in a top-level "instances" property, which would shadow the ' +
        'existing property of that name. Rename that property first.',
    });
  }

  if (typeof designated === 'string' && designated !== '') {
    const spec = properties[designated] as { type?: string; items?: { type?: string; $ref?: string; properties?: unknown } } | undefined;
    const available = Object.keys(properties).sort().join(', ') || 'none';
    if (!spec || typeof spec !== 'object') {
      errors.push({
        path: `/${X_AWS_IDP_INSTANCE_ARRAY}`,
        message: `Instance Array names "${designated}", which is not a property of this class. Available: ${available}.`,
      });
    } else if (spec.type !== 'array') {
      errors.push({
        path: `/${X_AWS_IDP_INSTANCE_ARRAY}`,
        message: `Instance Array names "${designated}", which is type "${spec.type ?? 'unset'}" — it must be an array, because each element is one document.`,
      });
    } else {
      const items = spec.items;
      const itemsAreObjects = Boolean(items) && (Boolean(items?.$ref) || items?.type === 'object' || Boolean(items?.properties));
      if (!itemsAreObjects) {
        errors.push({
          path: `/${X_AWS_IDP_INSTANCE_ARRAY}`,
          message: `Instance Array names "${designated}", whose items are not objects — each element must be an object representing one document.`,
        });
      }
    }
  } else if (designated !== undefined && designated !== null && designated !== '') {
    errors.push({
      path: `/${X_AWS_IDP_INSTANCE_ARRAY}`,
      message: 'Instance Array must be the name of a top-level array-of-objects property.',
    });
  }

  return errors;
};

export const useSchemaValidation = (): UseSchemaValidationReturn => {
  const [validationErrors, setSchemaValidationErrors] = useState<SchemaValidationError[]>([]);

  const ajv = useMemo(() => {
    const instance = new Ajv({
      allErrors: true, // nosemgrep: javascript.ajv.security.audit.ajv-allerrors-true.ajv-allerrors-true - allErrors required for comprehensive validation feedback for user created schemas in UI
      strict: false,
      validateFormats: true,
      discriminator: true,
      allowUnionTypes: true,
    });
    addFormats(instance);
    return instance;
  }, []);

  const validateSchema = useCallback(
    (schema: unknown): ValidationResult => {
      try {
        const validate = ajv.compile(EXTRACTION_JSON_SCHEMA);
        const valid = validate(schema);

        const idpErrors = validateIdpClassExtensions(schema);

        if (!valid && validate.errors) {
          const errors = validate.errors.map((error) => ({
            path: error.instancePath || '/',
            message: error.message || 'Validation error',
            keyword: error.keyword,
          }));
          return { valid: false, errors: [...errors, ...idpErrors] };
        }

        if (idpErrors.length > 0) {
          return { valid: false, errors: idpErrors };
        }

        return { valid: true, errors: [] };
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : 'Unknown validation error';
        const errors = [{ path: '/', message: errorMessage }];
        return { valid: false, errors };
      }
    },
    [ajv],
  );

  const validateAttribute = useCallback((attribute: unknown): ValidationResult => {
    const errors: SchemaValidationError[] = [];

    if (!attribute || typeof attribute !== 'object') {
      return { valid: false, errors: [{ path: '/', message: 'Attribute must be an object' }] };
    }

    const attr = attribute as ValidatableAttribute;

    if (!attr.type && !attr.$ref && !attr.oneOf && !attr.anyOf && !attr.allOf) {
      errors.push({ path: '/', message: 'Attribute must have a type, $ref, or composition keyword' });
    }

    if (attr.type === 'string') {
      if (attr.minLength !== undefined && attr.maxLength !== undefined) {
        if (attr.minLength > attr.maxLength) {
          errors.push({ path: '/minLength', message: 'minLength cannot be greater than maxLength' });
        }
      }
    }

    if (attr.type === 'number' || attr.type === 'integer') {
      if (attr.minimum !== undefined && attr.exclusiveMinimum !== undefined) {
        errors.push({ path: '/minimum', message: 'Cannot have both minimum and exclusiveMinimum' });
      }
      if (attr.maximum !== undefined && attr.exclusiveMaximum !== undefined) {
        errors.push({ path: '/maximum', message: 'Cannot have both maximum and exclusiveMaximum' });
      }
      if (attr.minimum !== undefined && attr.maximum !== undefined) {
        if (attr.minimum > attr.maximum) {
          errors.push({ path: '/minimum', message: 'minimum cannot be greater than maximum' });
        }
      }
    }

    if (attr.type === 'array') {
      if (attr.minItems !== undefined && attr.maxItems !== undefined) {
        if (attr.minItems > attr.maxItems) {
          errors.push({ path: '/minItems', message: 'minItems cannot be greater than maxItems' });
        }
      }
      if (attr.minContains !== undefined && attr.maxContains !== undefined) {
        if (attr.minContains > attr.maxContains) {
          errors.push({ path: '/minContains', message: 'minContains cannot be greater than maxContains' });
        }
      }
    }

    if (attr.type === 'object') {
      if (attr.minProperties !== undefined && attr.maxProperties !== undefined) {
        if (attr.minProperties > attr.maxProperties) {
          errors.push({ path: '/minProperties', message: 'minProperties cannot be greater than maxProperties' });
        }
      }
    }

    if (attr.const !== undefined && attr.enum !== undefined) {
      errors.push({ path: '/const', message: 'Cannot have both const and enum' });
    }

    if (attr.readOnly && attr.writeOnly) {
      errors.push({ path: '/readOnly', message: 'Cannot be both readOnly and writeOnly' });
    }

    return {
      valid: errors.length === 0,
      errors,
    };
  }, []);

  const detectCircularReferences = useCallback(
    (schema: unknown, visited: Set<string> = new Set(), path: string[] = []): SchemaValidationError[] => {
      if (!schema || typeof schema !== 'object') return [];

      const obj = schema as SchemaNode;
      const errors: SchemaValidationError[] = [];

      if (obj.$ref) {
        const refName = obj.$ref.replace('#/$defs/', '');

        if (visited.has(refName)) {
          errors.push({
            path: `/${path.join('/')}`,
            message: `Circular reference detected: ${[...visited, refName].join(' -> ')}`,
            keyword: 'circular-ref',
          });
          return errors;
        }

        return errors;
      }

      if (obj.properties) {
        Object.entries(obj.properties).forEach(([propName, propSchema]: [string, SchemaNode]) => {
          if (propSchema.$ref) {
            const newVisited = new Set([...visited, path.join('/')]);
            errors.push(...detectCircularReferences(propSchema, newVisited, [...path, propName]));
          }

          if (propSchema.properties) {
            errors.push(...detectCircularReferences(propSchema, visited, [...path, propName]));
          }

          if (propSchema.items) {
            errors.push(...detectCircularReferences(propSchema.items, visited, [...path, propName, 'items']));
          }
        });
      }

      return errors;
    },
    [],
  );

  const validateReferences = useCallback((schema: unknown, availableClasses: Array<{ name: string }> = []): SchemaValidationError[] => {
    const errors: SchemaValidationError[] = [];
    const classNames = new Set(availableClasses.map((cls) => cls.name));

    const checkRef = (ref: string, path: string) => {
      const className = ref.replace('#/$defs/', '');
      if (!classNames.has(className)) {
        errors.push({
          path,
          message: `Reference to undefined class: ${className}`,
          keyword: 'invalid-ref',
        });
      }
    };

    const traverse = (obj: unknown, path: string[] = []) => {
      if (!obj || typeof obj !== 'object') return;

      const o = obj as SchemaNode;

      if (o.$ref) {
        checkRef(o.$ref, `/${path.join('/')}`);
      }

      if (o.properties) {
        Object.entries(o.properties).forEach(([propName, propSchema]: [string, SchemaNode]) => {
          traverse(propSchema, [...path, propName]);
        });
      }

      if (o.items) {
        traverse(o.items, [...path, 'items']);
      }
    };

    traverse(schema);

    return errors;
  }, []);

  const clearErrors = useCallback((): void => {
    setSchemaValidationErrors([]);
  }, []);

  return {
    validateSchema,
    validateAttribute,
    detectCircularReferences,
    validateReferences,
    validationErrors,
    clearErrors,
  };
};
