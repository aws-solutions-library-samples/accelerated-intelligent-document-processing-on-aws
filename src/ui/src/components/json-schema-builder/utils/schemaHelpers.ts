import { TYPE_COLORS, TYPE_OBJECT } from '../../../constants/schemaConstants';

/** A property node as the schema builder holds it (structural, index-signature friendly). */
export interface AttributeLike {
  type?: string;
  $ref?: string;
  [key: string]: unknown;
}

/** A designer class — the in-memory stand-in for one `$defs` entry. */
export interface ClassLike {
  name: string;
  [key: string]: unknown;
}

const DEFS_PREFIX = '#/$defs/';

/**
 * The JSON Schema `type` of a property, following a local `#/$defs/<name>`
 * reference to its target.
 *
 * A property written as `{"$ref": "#/$defs/Address"}` carries no `type` of its
 * own — the type lives on the referenced definition — so reading `.type` off the
 * referencing node yields `undefined`. Anything that then compares that value
 * against a list of types silently treats the field as untyped (GitHub #906, the
 * same blind spot as #638 and #678 on the backend).
 *
 * The pointer is followed for real rather than approximated to `'object'`: the
 * designer holds every `$defs` target as an entry in its class list, so the
 * target's own declared type is readable, which stays correct if a `$ref` to a
 * non-object definition ever appears.
 *
 * Returns `undefined` when the type genuinely cannot be determined — a dangling
 * ref, a remote ref, or no class list supplied. Callers must decide what that
 * means for them; it does not mean "no type applies".
 */
export const resolveAttributeType = (
  attribute: AttributeLike | null | undefined,
  availableClasses?: ReadonlyArray<ClassLike>,
): string | undefined => {
  if (!attribute) return undefined;
  // A sibling `type` wins, as `$ref` composition works in draft 2020-12 and as
  // the inspector's Type dropdown already assumes. The designer itself never
  // writes that pair — `refAttributeUpdates` below clears `type` — so it only
  // arrives here from a hand-edited or older saved schema.
  if (typeof attribute.type === 'string' && attribute.type) return attribute.type;

  const ref = attribute.$ref;
  if (typeof ref !== 'string' || !ref.startsWith(DEFS_PREFIX)) return undefined;

  const targetName = ref.slice(DEFS_PREFIX.length);
  const target = (availableClasses ?? []).find((cls) => cls?.name === targetName);
  if (!target) return undefined;

  // The designer nests a class body under `attributes` (where the importer and
  // `addClass` record `type: 'object'`); an imported/exported class carries it at
  // the top. A class that is present but typeless is an object — `buildJSONSchema`
  // below writes every `$defs` entry as `type: 'object'`.
  const nested = (target.attributes as { type?: unknown } | undefined)?.type;
  const declared = typeof nested === 'string' ? nested : target.type;
  return typeof declared === 'string' && declared ? declared : TYPE_OBJECT;
};

/**
 * Keywords that describe an object defined *inline* and so must not sit beside a
 * `$ref`, which delegates the whole type designation to the referenced `$defs`
 * entry.
 *
 * `type` is in this list. A `$ref` with a sibling `type: 'object'` is legal draft
 * 2020-12 — both keywords apply — and redundant for every target the designer can
 * hold, since it writes every `$defs` entry as an object. What it costs is that a
 * node carrying it reads back differently from one without: `resolveAttributeType`
 * above prefers a sibling `type` over following the pointer, so the same attribute
 * answers differently depending on which route created it. Were a `$ref` to a
 * non-object definition ever to appear, the sibling would also be contradictory
 * rather than merely redundant.
 */
const INLINE_OBJECT_KEYWORDS = ['type', 'properties', 'required', 'minProperties', 'maxProperties', 'additionalProperties'] as const;

/**
 * The partial update that turns a property into a reference to a shared class.
 *
 * Every route that lets a user pick a reference class writes this one shape — the
 * Add Attribute modal and the inspector's "Reference Existing Class" picker — so
 * the same user-visible action produces the same JSON whichever way it is
 * reached (GitHub #957). Cleared keys are set to `undefined` rather than omitted
 * because `updateAttribute` treats an `undefined` value as "delete this key" and
 * an absent key as "leave it alone".
 */
export const refAttributeUpdates = (ref: string): Record<string, unknown> => {
  const updates: Record<string, unknown> = { $ref: ref };
  INLINE_OBJECT_KEYWORDS.forEach((keyword) => {
    updates[keyword] = undefined;
  });
  return updates;
};

const typeColorCache = new Map<string, string>();

export const getTypeColor = (type: string): string => {
  if (typeColorCache.has(type)) {
    return typeColorCache.get(type)!;
  }
  const color = TYPE_COLORS[type as keyof typeof TYPE_COLORS] || 'grey';
  typeColorCache.set(type, color);
  return color;
};

export const sanitizeAttribute = (attr: unknown): unknown => {
  if (!attr || typeof attr !== 'object') {
    return attr;
  }

  const cleaned: Record<string, unknown> = { ...(attr as Record<string, unknown>) };
  delete cleaned.id;
  delete cleaned.name;

  // A `$ref` delegates the type designation to the referenced `$defs` entry, so
  // the keywords describing an inline object go with it. Mirrors
  // `sanitizeAttributeSchema` in `useSchemaDesigner`, which is what the live
  // export path runs; a node that picked up a stray `type` (an older saved
  // schema, or a hand edit) is normalized by either.
  if (cleaned.$ref) {
    delete cleaned.type;
    delete cleaned.properties;
    delete cleaned.required;
  }

  if (cleaned.items) {
    cleaned.items = sanitizeAttribute(cleaned.items);
  }

  if (cleaned.properties) {
    const cleanedProperties: Record<string, unknown> = {};
    Object.entries(cleaned.properties as Record<string, unknown>).forEach(([key, value]) => {
      cleanedProperties[key] = sanitizeAttribute(value);
    });
    cleaned.properties = cleanedProperties;
  }

  return cleaned;
};

export const generateUniqueId = (prefix = 'item'): string => {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
};

export const isValidJSON = (str: string): boolean => {
  try {
    JSON.parse(str);
    return true;
  } catch {
    return false;
  }
};

export const safeParseJSON = (str: string, fallback: unknown = null): unknown => {
  try {
    return JSON.parse(str);
  } catch {
    return fallback;
  }
};

interface SchemaClassObj {
  name: string;
  description?: string;
  attributes: {
    properties?: Record<string, unknown>;
    required?: string[];
  };
}

export const buildJSONSchema = (classObj: SchemaClassObj, allClasses: SchemaClassObj[] = []): Record<string, unknown> => {
  const defs: Record<string, unknown> = {};

  allClasses.forEach((cls) => {
    const sanitizedProperties: Record<string, unknown> = {};
    Object.entries(cls.attributes?.properties || {}).forEach(([key, value]) => {
      sanitizedProperties[key] = sanitizeAttribute(value);
    });

    defs[cls.name] = {
      type: 'object',
      ...(cls.description ? { description: cls.description } : {}),
      properties: sanitizedProperties,
      ...(cls.attributes.required && cls.attributes.required.length > 0 ? { required: cls.attributes.required } : {}),
    };
  });

  const sanitizedProperties: Record<string, unknown> = {};
  Object.entries(classObj.attributes?.properties || {}).forEach(([key, value]) => {
    sanitizedProperties[key] = sanitizeAttribute(value);
  });

  return {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    $id: classObj.name,
    type: 'object',
    ...(classObj.description ? { description: classObj.description } : {}),
    properties: sanitizedProperties,
    ...(classObj.attributes.required && classObj.attributes.required.length > 0 ? { required: classObj.attributes.required } : {}),
    $defs: defs,
  };
};

export const formatValueForInput = (value: unknown): string => {
  if (value === undefined || value === null) return '';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
};

export const parseInputValue = (input: string, originalType = 'string'): unknown => {
  if (!input || !input.trim()) return undefined;

  if (originalType === 'object' || originalType === 'array') {
    return safeParseJSON(input, input);
  }

  if (originalType === 'number' || originalType === 'integer') {
    const num = parseFloat(input);
    return Number.isNaN(num) ? input : num;
  }

  return input;
};
