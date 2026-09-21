import React from 'react';
import { Header, FormField, Input, Checkbox, ExpandableSection, SpaceBetween } from '@cloudscape-design/components';
import ContainsSchemaBuilder from './ContainsSchemaBuilder';

interface SchemaAttribute {
  type?: string;
  minItems?: number;
  maxItems?: number;
  uniqueItems?: boolean;
  contains?: Record<string, unknown>;
  minContains?: number;
  maxContains?: number;
  [key: string]: unknown;
}

interface AvailableClass {
  name: string;
  id?: string;
  description?: string;
}

interface ArrayConstraintsProps {
  attribute: SchemaAttribute;
  onUpdate: (updates: Partial<SchemaAttribute>) => void;
  availableClasses?: AvailableClass[];
}

const ArrayConstraints = ({ attribute, onUpdate, availableClasses = [] }: ArrayConstraintsProps): React.JSX.Element | null => {
  if (attribute.type !== 'array') return null;

  const handleContainsSchemaChange = (schema: Record<string, unknown> | null): void => {
    if (!schema || Object.keys(schema).length === 0) {
      const updates = { ...attribute };
      delete updates.contains;
      delete updates.minContains;
      delete updates.maxContains;
      onUpdate(updates);
      return;
    }

    onUpdate({ contains: schema });
  };

  return (
    <>
      <Header {...({ variant: 'h4' } as Record<string, unknown>)}>Array Constraints</Header>

      <FormField
        label="Min Items"
        description={
          'Minimum number of items required in the array. Leave empty for no floor. In Advanced extraction this is a hard floor enforced ' +
          'while the agent works — and per shard when the section is sharded, which it is on the shipped defaults — so a floor a ' +
          'document (or one shard) cannot reach fails extraction and keeps no rows. Set only a floor you are willing to fail on; to ' +
          'be warned instead, leave this empty and rely on the extraction setting "Truncated list outcome" (default: warn), which is ' +
          'evaluated once on the merged section and keeps the rows it did extract.'
        }
      >
        <Input
          type="number"
          value={attribute.minItems?.toString() || ''}
          onChange={({ detail }) => onUpdate({ minItems: detail.value ? parseInt(detail.value, 10) : undefined })}
          placeholder="empty = no floor"
        />
      </FormField>

      <FormField label="Max Items" description="Maximum number of items allowed in the array. Leave empty for unlimited.">
        <Input
          type="number"
          value={attribute.maxItems?.toString() || ''}
          onChange={({ detail }) => onUpdate({ maxItems: detail.value ? parseInt(detail.value, 10) : undefined })}
          placeholder="e.g., 10"
        />
      </FormField>

      <Checkbox checked={attribute.uniqueItems || false} onChange={({ detail }) => onUpdate({ uniqueItems: detail.checked || undefined })}>
        Unique Items (all array elements must be unique, no duplicates allowed)
      </Checkbox>

      <ExpandableSection headerText="Advanced: Pattern Matching (Contains)" variant="footer">
        <SpaceBetween size="m">
          <ContainsSchemaBuilder
            containsSchema={attribute.contains}
            onChange={handleContainsSchemaChange}
            availableClasses={availableClasses}
          />

          {attribute.contains && (
            <>
              <FormField
                label="Min Contains"
                description="Minimum number of items that must match the contains schema. Leave empty for at least 1."
              >
                <Input
                  type="number"
                  value={attribute.minContains?.toString() || ''}
                  onChange={({ detail }) =>
                    onUpdate({
                      minContains: detail.value ? parseInt(detail.value, 10) : undefined,
                    })
                  }
                  placeholder="e.g., 2"
                />
              </FormField>

              <FormField
                label="Max Contains"
                description="Maximum number of items that can match the contains schema. Leave empty for unlimited."
              >
                <Input
                  type="number"
                  value={attribute.maxContains?.toString() || ''}
                  onChange={({ detail }) =>
                    onUpdate({
                      maxContains: detail.value ? parseInt(detail.value, 10) : undefined,
                    })
                  }
                  placeholder="e.g., 5"
                />
              </FormField>
            </>
          )}
        </SpaceBetween>
      </ExpandableSection>
    </>
  );
};

export default ArrayConstraints;
