# A ~110-token block of GENERIC extraction guidance. Deliberately not filler: it is
# content a model can act on, applies to any class, and would be reasonable to add to
# a task prompt on its own merits. Padding with real guidance rather than noise is the
# whole point — a prompt padded with junk is a quality risk taken to win a cache.
PAD = """
    <output-discipline>
    Apply these rules to every field you return:
    - Copy values exactly as they appear in the source; do not normalise, round,
      re-order or re-word them unless the field's description asks you to.
    - Where a field is absent from the document, return null. Do not infer it from
      context, from another field, or from what is typical for this document type.
    - Where a value is present but partly illegible, return the legible portion
      rather than guessing the remainder, and never substitute a plausible value.
    - Do not add fields that the schema does not declare, and do not nest declared
      fields under a wrapper key.
    - Prefer the most specific occurrence when a value appears more than once, and
      the value from the document body over a value from a header or footer.
    </output-discipline>
"""
