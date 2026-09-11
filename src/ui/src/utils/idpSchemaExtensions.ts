import { SchemaValidationError } from '../types/common';
import { X_AWS_IDP_INSTANCE_ARRAY, X_AWS_IDP_MULTI_INSTANCE } from '../constants/schemaConstants';

/**
 * Client-side PREFLIGHT for the class-level `x-aws-idp-*` rules the BACKEND
 * hard-rejects at config load.
 *
 * Ajv only checks standard JSON-Schema keywords, so before this an invalid
 * `x-aws-idp-instance-array` was displayed with red `errorText` in the inspector
 * and then saved anyway — the message said "will be rejected on save" and Save
 * worked. The rejection arrived later, from the server, on a config the user had
 * been told was a problem.
 *
 * Not a second source of truth: `IDPConfig` stays authoritative and these mirror
 * `validate_instance_array` in `idp_common/config/models.py`. Keep the two in
 * step — and note the asymmetry: being **stricter** than the backend is the
 * dangerous direction, because this gate blocks Save, so a schema the pipeline
 * would have accepted becomes one the user cannot store at all. Both checks below
 * therefore reproduce the backend's leniency deliberately.
 *
 * It lives here rather than in `useSchemaValidation` so the inspector's inline
 * warning and the save gate are literally the same predicate. They used to be two
 * hand-kept copies, and the inline one was stricter — it told the user a valid
 * designation "will be rejected on save" while Save succeeded.
 */

/**
 * Resolve a local `#/$defs/<name>` `$ref`, mirroring `deref_schema` in
 * `idp_common/config/schema_utils.py`.
 *
 * Needed for correctness, not tidiness: `{"$ref": "#/$defs/RecordList"}` is the
 * idiom this editor emits for a reusable record type and that several shipped
 * presets use, and the un-dereferenced node has no `type` at all. Type-checking it
 * directly reports 'type "unset" — it must be an array' on a schema the backend
 * dereferences and accepts.
 *
 * An unresolvable ref (remote, dangling, cyclic, or `$defs` simply absent —
 * which is the case in the designer's in-memory shape, where `$defs` is only
 * synthesized at export time) returns the node as-is, so it degrades to the
 * un-dereferenced check exactly as the backend does.
 */
export const derefLocal = (node: unknown, root: Record<string, unknown>, seen = new Set<string>()): Record<string, unknown> => {
  if (!node || typeof node !== 'object') return {};
  const asObj = node as Record<string, unknown>;
  const ref = asObj.$ref;
  if (typeof ref !== 'string') return asObj;
  const prefix = '#/$defs/';
  if (!ref.startsWith(prefix) || seen.has(ref)) return asObj;
  const defs = root.$defs;
  if (!defs || typeof defs !== 'object') return asObj;
  const target = (defs as Record<string, unknown>)[ref.slice(prefix.length)];
  if (!target || typeof target !== 'object') return asObj;
  seen.add(ref);
  // Sibling keys on the referencing node layer on top, as the backend does.
  const { $ref: _drop, ...siblings } = asObj;
  return { ...derefLocal(target, root, seen), ...siblings };
};

const isTruthyFlag = (raw: unknown): boolean =>
  raw === true || (typeof raw === 'string' && ['true', 'yes', '1'].includes(raw.trim().toLowerCase()));

/** The designer nests properties under `attributes`; an exported class has them at the top. */
const readShape = (cls: Record<string, unknown>): { properties: Record<string, unknown>; defsRoot: Record<string, unknown> } => {
  const attributes = cls.attributes as { properties?: Record<string, unknown>; $defs?: unknown } | undefined;
  const properties = (attributes?.properties ?? (cls.properties as Record<string, unknown>) ?? {}) as Record<string, unknown>;
  // `$defs` sits beside `properties`, so it is under `attributes` in the designer
  // shape and at the top level in an exported class.
  return { properties, defsRoot: { $defs: attributes?.$defs ?? cls.$defs } };
};

/**
 * Why `x-aws-idp-instance-array: <name>` on this class would be rejected, or
 * `null` if it would be accepted.
 *
 * Returns the reason as prose so the caller can render it either as a save-blocking
 * validation error or as inline `errorText` on the picker — the point being that
 * both come from here.
 */
export const designationProblem = (cls: unknown, designated: string): string | null => {
  if (!cls || typeof cls !== 'object' || !designated) return null;
  const { properties, defsRoot } = readShape(cls as Record<string, unknown>);
  const raw = properties[designated];
  if (!raw || typeof raw !== 'object') {
    const available = Object.keys(properties).sort().join(', ') || 'none';
    return `Instance Array names "${designated}", which is not a property of this class. Available: ${available}.`;
  }
  const spec = derefLocal(raw, defsRoot);
  const specType = spec.type;
  if (specType !== 'array') {
    return `Instance Array names "${designated}", which is type "${typeof specType === 'string' ? specType : 'unset'}" — it must be an array, because each element is one document.`;
  }
  // Reject ONLY an inline `items` that is explicitly a non-object, exactly as the
  // backend does. `items` absent, `$ref`'d, or typeless (`oneOf`/`allOf`, or just
  // `{properties: …}`) is resolved at runtime and must not be reported as invalid.
  const items = spec.items;
  if (items && typeof items === 'object') {
    const itemsType = derefLocal(items, defsRoot).type;
    if (typeof itemsType === 'string' && itemsType !== 'object') {
      return `Instance Array names "${designated}", whose items are type "${itemsType}" — each element must be an object representing one document.`;
    }
  }
  return null;
};

export const validateIdpClassExtensions = (schema: unknown): SchemaValidationError[] => {
  if (!schema || typeof schema !== 'object') return [];
  const cls = schema as Record<string, unknown>;
  const errors: SchemaValidationError[] = [];
  const { properties } = readShape(cls);

  const designated = cls[X_AWS_IDP_INSTANCE_ARRAY];
  const multi = isTruthyFlag(cls[X_AWS_IDP_MULTI_INSTANCE]);

  // The synthesized wrapper legitimately carries BOTH keys. It is produced at
  // runtime and never stored, but rejecting a schema the pipeline itself emits
  // would be a nasty trap for anyone who round-trips one.
  const isWrapper = multi && Object.keys(properties).length === 1 && 'instances' in properties;
  if (isWrapper) return [];

  const hasDesignation = designated !== undefined && designated !== null && designated !== '';

  if (multi && hasDesignation) {
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
    const problem = designationProblem(cls, designated);
    if (problem) {
      errors.push({ path: `/${X_AWS_IDP_INSTANCE_ARRAY}`, message: problem });
    }
  } else if (hasDesignation) {
    errors.push({
      path: `/${X_AWS_IDP_INSTANCE_ARRAY}`,
      message: 'Instance Array must be the name of a top-level array-of-objects property.',
    });
  }

  return errors;
};
