import {
  TYPE_COLORS,
  TYPE_OBJECT,
  SUBSCHEMA_KEYWORDS,
  SUBSCHEMA_MAP_KEYWORDS,
  DESIGNER_ONLY_KEYS,
  OBJECT_BODY_KEYWORDS,
  INLINE_OBJECT_KEYWORDS,
  REF_INCOMPATIBLE_KEYWORDS,
} from '../../../constants/schemaConstants';

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

  // The designer nests a class body under `attributes` (where the importer records the
  // definition's own type and `addClass` records `type: 'object'`); an imported/exported
  // class carries it at the top. A class that is present but typeless is an object, which
  // is also how `buildJSONSchema` below writes such a `$defs` entry out.
  const nested = (target.attributes as { type?: unknown } | undefined)?.type;
  const declared = typeof nested === 'string' ? nested : target.type;
  return typeof declared === 'string' && declared ? declared : TYPE_OBJECT;
};

/**
 * Whether a `$defs` body says it is something other than an object.
 *
 * The question three places need answering and must answer the same way: the importer and
 * the exporter, deciding whether to supply `type: "object"` for a body that declares no
 * type, and `addAttribute`, deciding whether giving the body a property converts it.
 *
 * Allow-list, not deny-list — see `OBJECT_BODY_KEYWORDS`. A declared non-object `type`
 * settles it outright; otherwise any keyword that is not object-applicable does.
 */
export const declaresNonObjectShape = (body: Record<string, unknown> | undefined | null): boolean => {
  if (!body) return false;
  const declared = body.type;
  if (typeof declared === 'string' && declared && declared !== TYPE_OBJECT) return true;
  return Object.keys(body).some((keyword) => !keyword.startsWith('x-') && !(OBJECT_BODY_KEYWORDS as readonly string[]).includes(keyword));
};

/**
 * The keywords to strip when a body that was not an object becomes one. Complete by
 * construction: everything the allow-list does not cover described the old shape.
 */
export const nonObjectBodyKeywords = (body: Record<string, unknown>): string[] =>
  Object.keys(body).filter((keyword) => !keyword.startsWith('x-') && !(OBJECT_BODY_KEYWORDS as readonly string[]).includes(keyword));

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

/**
 * A subschema that is nothing but a reference to a shared class.
 *
 * `refAttributeUpdates` is a partial *update*, carrying explicit `undefined`s that tell
 * `updateAttribute` to delete the inline keywords the reference replaces. This is the
 * *value* form, for the positions where a whole subschema is written at once and there is
 * nothing to clear: an array's `items`, a `contains`, a composition branch, a conditional
 * branch. Every such write goes through here, and
 * `__tests__/refWriterInventory.test.ts` fails on a new one that does not — an
 * enumeration of the writers in a comment went stale within one release, so the guard has
 * to be mechanical.
 *
 * Takes either a bare class name or an already-built pointer, because the pickers that
 * call it carry both conventions.
 */
export const refNode = (classNameOrPointer: string): { $ref: string } => ({
  $ref: classNameOrPointer.startsWith(DEFS_PREFIX) ? classNameOrPointer : `${DEFS_PREFIX}${classNameOrPointer}`,
});

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
  if (Array.isArray(attr)) {
    return attr.map((entry) => sanitizeAttribute(entry));
  }

  if (!attr || typeof attr !== 'object') {
    return attr;
  }

  const cleaned: Record<string, unknown> = { ...(attr as Record<string, unknown>) };
  DESIGNER_ONLY_KEYS.forEach((key) => delete cleaned[key]);

  // A `$ref` delegates the type designation to the referenced `$defs` entry, so the
  // keywords that would contradict it go with it. Mirrors `sanitizeAttributeSchema` in
  // `useSchemaDesigner`, which is what the live export path runs, and uses the narrow list
  // rather than `refAttributeUpdates`' write list: `minProperties`, `maxProperties` and
  // `additionalProperties` beside a `$ref` are the documented way to constrain a
  // reference, and this runs over hand-authored nodes too.
  if (cleaned.$ref) {
    REF_INCOMPATIBLE_KEYWORDS.forEach((keyword) => delete cleaned[keyword]);
  }

  SUBSCHEMA_KEYWORDS.forEach((keyword) => {
    const value = cleaned[keyword];
    if (value && typeof value === 'object') {
      cleaned[keyword] = sanitizeAttribute(value);
    }
  });

  SUBSCHEMA_MAP_KEYWORDS.forEach((keyword) => {
    const map = cleaned[keyword];
    if (!map || typeof map !== 'object') return;
    const cleanedMap: Record<string, unknown> = {};
    Object.entries(map as Record<string, unknown>).forEach(([key, value]) => {
      cleanedMap[key] = sanitizeAttribute(value);
    });
    cleaned[keyword] = cleanedMap;
  });

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
    type?: string;
    properties?: Record<string, unknown>;
    required?: string[];
    [key: string]: unknown;
  };
}

export const buildJSONSchema = (classObj: SchemaClassObj, allClasses: SchemaClassObj[] = []): Record<string, unknown> => {
  const defs: Record<string, unknown> = {};

  allClasses.forEach((cls) => {
    const sanitizedProperties: Record<string, unknown> = {};
    Object.entries(cls.attributes?.properties || {}).forEach(([key, value]) => {
      sanitizedProperties[key] = sanitizeAttribute(value);
    });

    // A `$defs` entry declares its own type; it is not necessarily an object, and no type
    // is invented for a body that already describes its own shape. Mirrors `exportSchema`
    // in `useSchemaDesigner`, which is what the live save path runs.
    const { type: bodyType, properties: _bodyProps, required: bodyRequired, ...bodyConstraints } = cls.attributes || {};
    const definitionType = bodyType || (declaresNonObjectShape(bodyConstraints) ? null : TYPE_OBJECT);

    defs[cls.name] = sanitizeAttribute({
      ...(definitionType ? { type: definitionType } : {}),
      ...(cls.description ? { description: cls.description } : {}),
      ...bodyConstraints,
      ...(definitionType === TYPE_OBJECT || Object.keys(sanitizedProperties).length > 0 ? { properties: sanitizedProperties } : {}),
      ...(bodyRequired && bodyRequired.length > 0 ? { required: bodyRequired } : {}),
    });
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
