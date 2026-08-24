import React, { useState, useEffect, useRef, useMemo } from 'react';
import {
  Container,
  SpaceBetween,
  Header,
  Button,
  ButtonDropdown,
  Box,
  Alert,
  ColumnLayout,
  Modal,
  FormField,
  Input,
  Select,
  Textarea,
} from '@cloudscape-design/components';
import type { InputProps } from '@cloudscape-design/components';
import { useSchemaDesigner } from '../../hooks/useSchemaDesigner';
import { useSchemaValidation } from '../../hooks/useSchemaValidation';
import { useDebounce } from '../../hooks/useDebounce';
import { TYPE_OPTIONS, X_AWS_IDP_DOCUMENT_TYPE, X_AWS_IDP_RULE_ID } from '../../constants/schemaConstants';
import SchemaCanvas from './SchemaCanvas';
import SchemaInspector from './SchemaInspector';
import SchemaPreviewTabs from './SchemaPreviewTabs';
import StandardClassCatalog from './StandardClassCatalog';
import { formatTypeBadge, DocumentTypeBadge } from './utils/badgeHelpers';

interface SchemaClass {
  id: string;
  name: string;
  description?: string;
  attributes?: {
    properties?: Record<string, Record<string, unknown>>;
    required?: string[];
  };
  [key: string]: unknown;
}

interface ValidationError {
  path: string;
  message: string;
  keyword?: string;
  className?: string;
}

interface TypeOption {
  label: string;
  value: string;
}

interface SchemaBuilderProps {
  initialSchema?: SchemaClass[] | Record<string, unknown> | null;
  onChange?: ((schema: unknown, isDirty: boolean) => void) | null;
  onValidate?: ((isValid: boolean, errors: ValidationError[]) => void) | null;
  isRuleSchema?: boolean;
}

const SchemaBuilder = ({
  initialSchema = null,
  onChange = null,
  onValidate = null,
  isRuleSchema = false,
  highlightClassName = null,
}: SchemaBuilderProps & { highlightClassName?: string | null }): React.JSX.Element => {
  const {
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
    getSelectedClass,
    getSelectedAttribute,
    clearAllClasses,
  } = useSchemaDesigner(initialSchema || [], isRuleSchema);

  const { validateSchema } = useSchemaValidation();

  const [showPreview, setShowPreview] = useState(false);
  const [showAddClassModal, setShowAddClassModal] = useState(false);
  const [addClassMode, setAddClassMode] = useState<'choose' | 'custom' | 'standard'>('choose');
  const [showAddAttributeModal, setShowAddAttributeModal] = useState(false);
  const [newClassName, setNewClassName] = useState('');
  const [newClassDescription, setNewClassDescription] = useState('');
  const [newAttributeName, setNewAttributeName] = useState('');
  const [newAttributeType, setNewAttributeType] = useState<TypeOption>({ label: 'String', value: 'string' });
  const [newAttributeDescription, setNewAttributeDescription] = useState('');
  const [newAttributeReferenceClass, setNewAttributeReferenceClass] = useState<TypeOption | null>(null);
  const [showEditClassModal, setShowEditClassModal] = useState(false);
  const [editingClass, setEditingClass] = useState<SchemaClass | null>(null);
  const [showDeleteConfirmModal, setShowDeleteConfirmModal] = useState(false);
  const [classToDelete, setClassToDelete] = useState<SchemaClass | null>(null);
  const [showWipeAllModal, setShowWipeAllModal] = useState(false);
  const [aggregatedValidationErrors, setAggregatedValidationErrors] = useState<ValidationError[]>([]);

  const CLASS_NAME_PATTERN = /^[a-zA-Z0-9\-_]+$/;
  const isClassNameValid = (name: string): boolean => name.trim().length > 0 && CLASS_NAME_PATTERN.test(name.trim());
  const classNameErrorText =
    newClassName.trim() && !CLASS_NAME_PATTERN.test(newClassName.trim())
      ? 'Class name can only contain letters, numbers, hyphens, and underscores'
      : '';

  const lastExportedSchemaRef = useRef<string | null>(null);
  const lastValidationResultRef = useRef<string | null>(null);

  // Debounce classes changes to reduce validation frequency
  const debouncedClasses = useDebounce(classes, 300);

  // Memoize the exported schema to avoid recalculating on every render
  const currentSchema = useMemo(() => exportSchema(), [exportSchema]);

  useEffect(() => {
    if (onChange) {
      const schemaString = JSON.stringify(currentSchema);
      if (schemaString !== lastExportedSchemaRef.current) {
        lastExportedSchemaRef.current = schemaString;
        onChange(currentSchema, isDirty);
      }
    }
  }, [currentSchema, isDirty, onChange]);

  // When the caller passes a highlightClassName (e.g. from the Policy Discovery
  // job's "View in Configuration" link), auto-select the matching class and
  // scroll its card into view so the user lands on the newly extracted rules.
  const highlightAppliedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!highlightClassName || classes.length === 0) return;
    if (highlightAppliedRef.current === highlightClassName) return;
    const match = classes.find((c) => c.name === highlightClassName);
    if (!match) return;
    setSelectedClassId(match.id);
    highlightAppliedRef.current = highlightClassName;
    // Defer scroll until after DOM paints the selection outline
    setTimeout(() => {
      const el = document.querySelector(`[data-schema-class-id="${match.id}"]`);
      if (el instanceof HTMLElement) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }, 100);
  }, [highlightClassName, classes, setSelectedClassId]);

  useEffect(() => {
    if (onValidate && debouncedClasses.length > 0) {
      const allErrors: ValidationError[] = [];
      let allValid = true;

      debouncedClasses.forEach((cls) => {
        const result = validateSchema(cls);
        if (!result.valid) {
          allValid = false;
          allErrors.push(...result.errors.map((err: ValidationError) => ({ ...err, className: cls.name })));
        }
      });

      const validationResult = JSON.stringify({ allValid, allErrors });
      if (validationResult !== lastValidationResultRef.current) {
        lastValidationResultRef.current = validationResult;
        setAggregatedValidationErrors(allErrors);
        onValidate(allValid, allErrors);
      }
    } else if (onValidate && debouncedClasses.length === 0) {
      if (lastValidationResultRef.current !== null) {
        lastValidationResultRef.current = null;
        setAggregatedValidationErrors([]);
        onValidate(true, []);
      }
    }
  }, [debouncedClasses, onValidate, validateSchema]);

  const handleAddClass = (): void => {
    setAddClassMode(isRuleSchema ? 'custom' : 'choose');
    setShowAddClassModal(true);
  };

  const handleAddAttribute = (): void => {
    if (!selectedClassId) {
      alert('Please select a class first');
      return;
    }
    setShowAddAttributeModal(true);
  };

  const handleConfirmAddClass = (): void => {
    if (newClassName.trim()) {
      addClass(newClassName.trim(), newClassDescription.trim() || undefined);
      setNewClassName('');
      setNewClassDescription('');
      setShowAddClassModal(false);
    }
  };

  // Ref to the Add Attribute name input so "Add another" can refocus it.
  const addAttributeNameRef = useRef<InputProps.Ref>(null);

  // Add the attribute currently described in the modal. When `keepOpen` is true,
  // the modal stays open with the form reset and focus returned to the name field
  // ("Save and Add Another") so multiple attributes can be added in a row.
  const handleConfirmAddAttribute = (keepOpen = false): void => {
    if (newAttributeName.trim() && newAttributeType.value && selectedClassId) {
      const attrName = newAttributeName.trim();
      addAttribute(selectedClassId, attrName, newAttributeType.value);

      const updates: Record<string, unknown> = {};
      if (newAttributeDescription.trim()) {
        updates.description = newAttributeDescription.trim();
      }

      // Auto-generate rule_id for rule schemas from the attribute name
      if (isRuleSchema) {
        let ruleId = attrName
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, '_')
          .replace(/^_|_$/g, '');
        // Fallback if slug is empty (e.g. all-punctuation name)
        if (!ruleId) {
          ruleId = `rule_${Date.now()}`;
        }
        // Ensure uniqueness within the class
        const selectedClass = classes.find((c) => c.id === selectedClassId);
        if (selectedClass) {
          const existingIds = new Set(
            Object.values(selectedClass.attributes?.properties || {}).map((p: Record<string, unknown>) => p[X_AWS_IDP_RULE_ID] as string),
          );
          let suffix = 2;
          const baseId = ruleId;
          while (existingIds.has(ruleId)) {
            ruleId = `${baseId}_${suffix}`;
            suffix++;
          }
        }
        updates[X_AWS_IDP_RULE_ID] = ruleId;
      }

      // If object or array and a reference class is selected, add $ref
      if (newAttributeReferenceClass && newAttributeReferenceClass.value) {
        if (newAttributeType.value === 'object') {
          updates.$ref = `#/$defs/${newAttributeReferenceClass.value}`;
          // Remove schema keywords that conflict with $ref
          updates.type = undefined;
          updates.properties = undefined;
          updates.required = undefined;
        } else if (newAttributeType.value === 'array') {
          updates.items = { $ref: `#/$defs/${newAttributeReferenceClass.value}` };
        }
      }

      if (Object.keys(updates).length > 0 && selectedClassId) {
        updateAttribute(selectedClassId, attrName, updates);
      }

      // Reset the form for the next entry.
      setNewAttributeName('');
      setNewAttributeType({ label: 'String', value: 'string' });
      setNewAttributeDescription('');
      setNewAttributeReferenceClass(null);

      if (keepOpen) {
        // Keep the modal open and return focus to the name field for fast entry.
        setTimeout(() => addAttributeNameRef.current?.focus(), 0);
      } else {
        setShowAddAttributeModal(false);
      }
    }
  };

  // Use TYPE_OPTIONS from constants, filtering to commonly used types
  const attributeTypeOptions = TYPE_OPTIONS.filter((opt) => ['string', 'number', 'boolean', 'object', 'array'].includes(opt.value));

  const handleEditClass = (cls: SchemaClass): void => {
    setEditingClass(cls);
    setNewClassName(cls.name);
    setNewClassDescription(cls.description || '');
    setShowEditClassModal(true);
  };

  const handleConfirmEditClass = (): void => {
    if (editingClass && newClassName.trim()) {
      updateClass(editingClass.id, {
        name: newClassName.trim(),
        description: newClassDescription.trim() || undefined,
      });
      setEditingClass(null);
      setNewClassName('');
      setNewClassDescription('');
      setShowEditClassModal(false);
    }
  };

  // Download a schema array as a pretty-printed JSON file.
  const downloadSchemaJson = (schema: unknown, filename: string): void => {
    const blob = new Blob([JSON.stringify(schema, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  // Export all document types (current behavior).
  const handleExportAll = (): void => {
    const schema = exportSchema();
    if (schema) {
      downloadSchemaJson(schema, `schema-${Date.now()}.json`);
    }
  };

  // Export only the currently-selected document type (plus its referenced shared
  // classes). Selected class must be a document type to be an exportable unit.
  const handleExportSelected = (): void => {
    const cls = getSelectedClass();
    if (!cls) return;
    const schema = exportSchema([cls.name as string]);
    if (schema) {
      const safeName = String(cls.name).replace(/[^a-zA-Z0-9._-]/g, '_');
      downloadSchemaJson(schema, `schema-${safeName}-${Date.now()}.json`);
    }
  };

  // Whether the current selection is a document type (only doc types export cleanly
  // as a standalone schema; shared classes are exported via the doc types that use them).
  const selectedClassForExport = getSelectedClass();
  const selectedIsDocType = Boolean(selectedClassForExport && selectedClassForExport[X_AWS_IDP_DOCUMENT_TYPE]);

  const handleWipeAll = (): void => {
    setShowWipeAllModal(true);
  };

  const handleConfirmWipeAll = (): void => {
    clearAllClasses();
    setShowWipeAllModal(false);
  };

  const docTypeCount = classes.filter((c) => c[X_AWS_IDP_DOCUMENT_TYPE]).length;
  const _sharedCount = classes.filter((c) => !c[X_AWS_IDP_DOCUMENT_TYPE]).length;

  // Dynamic labels based on schema type. Underscore-prefixed entries are part
  // of the full naming set but not yet referenced in this component; kept here
  // (opted out of the unused-vars lint rule) so the label vocabulary stays
  // colocated and ready for use.
  const typeLabel = isRuleSchema ? 'policy' : 'document';
  const _typeLabelPlural = isRuleSchema ? 'rules' : 'documents';
  const TypeLabel = isRuleSchema ? 'Rule' : 'Document';
  const TypesLabel = isRuleSchema ? 'Rules' : 'Documents';
  const _classLabel = isRuleSchema ? 'Policy Class' : 'Class';
  const classesLabel = isRuleSchema ? 'Policy Classes' : 'Classes';
  const _attributeLabel = isRuleSchema ? 'Rule' : 'Attribute';
  const attributesLabel = isRuleSchema ? 'Rules' : 'Attributes';
  const sharedLabel = isRuleSchema ? 'Recommendation Options' : 'Shared Classes';

  return (
    <>
      {/* Floating breadcrumb bar showing current selection - fixed to viewport */}
      {(selectedClassId || selectedAttributeId) && (
        <div
          style={{
            position: 'sticky',
            top: 0,
            zIndex: 1000,
            backgroundColor: '#ffffff',
            borderBottom: '2px solid #0972d3',
            padding: '12px 20px',
            boxShadow: '0 2px 4px rgba(0,0,0,0.1)',
            marginBottom: '16px',
          }}
        >
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            {selectedClassId && (
              <>
                <Box fontSize="body-m" fontWeight="bold">
                  {getSelectedClass()?.name || 'Unknown Class'}
                </Box>
                {getSelectedClass()?.[X_AWS_IDP_DOCUMENT_TYPE] && <DocumentTypeBadge isRuleSchema={isRuleSchema} />}
              </>
            )}
            {selectedAttributeId && (
              <>
                <Box fontSize="body-s" color="text-body-secondary">
                  ›
                </Box>
                <Box fontSize="body-m" color="text-label">
                  {selectedAttributeId}
                </Box>
                {getSelectedAttribute() && formatTypeBadge(getSelectedAttribute())}
              </>
            )}
            <Box {...({ flex: '1' } as Record<string, unknown>)} />
            <Button
              variant="inline-link"
              iconName="close"
              onClick={() => {
                setSelectedAttributeId(null);
                if (!selectedAttributeId) {
                  setSelectedClassId(null);
                }
              }}
              ariaLabel="Clear selection"
            >
              {selectedAttributeId ? 'Deselect attribute' : 'Deselect class'}
            </Button>
          </SpaceBetween>
        </div>
      )}

      <SpaceBetween size="l">
        {aggregatedValidationErrors.length > 0 && (
          <Alert type="error" dismissible onDismiss={() => setAggregatedValidationErrors([])} header="Schema Validation Errors">
            <ul>
              {aggregatedValidationErrors.map((error) => (
                <li key={`${error.className || 'unknown'}-${error.path}-${error.message}-${error.keyword || ''}`}>
                  {error.className && <strong>{error.className}</strong>} {error.path}: {error.message}
                </li>
              ))}
            </ul>
          </Alert>
        )}

        <div style={{ maxHeight: 'calc(100vh - 200px)', overflow: 'auto' }}>
          <Container
            header={
              <Header
                variant="h2"
                actions={
                  <Box>
                    <SpaceBetween direction="horizontal" size="xs">
                      <Button onClick={handleAddClass} iconName="add-plus">
                        {isRuleSchema ? 'Add Policy Class' : 'Add Class'}
                      </Button>
                      <Button onClick={handleAddAttribute} disabled={!selectedClassId} iconName="add-plus">
                        {isRuleSchema ? 'Add Rule' : 'Add Attribute'}
                      </Button>
                      <Button onClick={() => setShowPreview(!showPreview)} iconName={showPreview ? 'view-vertical' : 'view-horizontal'}>
                        {showPreview ? 'Hide' : 'Show'} Preview
                      </Button>
                      <ButtonDropdown
                        items={[
                          { id: 'export-all', text: 'Export all', iconName: 'download' },
                          {
                            id: 'export-selected',
                            text: selectedClassForExport ? `Export "${selectedClassForExport.name}"` : 'Export selected',
                            iconName: 'download',
                            disabled: !selectedIsDocType,
                            disabledReason: selectedClassForExport
                              ? 'Only document types export as a standalone schema. Select a document type (shared classes are included via the document types that reference them).'
                              : `Select a ${typeLabel} type to export it on its own.`,
                          },
                        ]}
                        onItemClick={({ detail }) => {
                          if (detail.id === 'export-all') handleExportAll();
                          else if (detail.id === 'export-selected') handleExportSelected();
                        }}
                        disabled={classes.length === 0}
                      >
                        Export
                      </ButtonDropdown>
                      <Button onClick={handleWipeAll} iconName="remove" disabled={classes.length === 0}>
                        Wipe All
                      </Button>
                    </SpaceBetween>
                  </Box>
                }
                description="Build JSON Schema Draft 2020-12 compliant extraction schemas with advanced features"
              >
                Schema Builder
              </Header>
            }
          >
            <ColumnLayout columns={showPreview ? 2 : 3} variant="text-grid" minColumnWidth={300}>
              <Box>
                <SpaceBetween size="m">
                  <Header variant="h3">
                    {classesLabel} ({docTypeCount} {typeLabel} {docTypeCount === 1 ? 'type' : 'types'})
                  </Header>

                  {classes.filter((c) => c[X_AWS_IDP_DOCUMENT_TYPE]).length === 0 && (
                    <Alert type="warning" header={`No ${TypesLabel} Types`}>
                      No classes are marked as {typeLabel} types. Mark at least one class as a {typeLabel} type to generate {typeLabel}{' '}
                      schemas.
                    </Alert>
                  )}

                  <Box>
                    {!isRuleSchema && <Header {...({ variant: 'h4' } as Record<string, unknown>)}>{TypesLabel} Types</Header>}
                    <SpaceBetween size="s">
                      {classes.filter((c) => c[X_AWS_IDP_DOCUMENT_TYPE]).length === 0 && (
                        <Box fontSize="body-s" color="text-body-secondary" padding="s">
                          No {typeLabel} types yet. Add a class and mark it as a {typeLabel} type.
                        </Box>
                      )}
                      {classes
                        .filter((cls) => cls[X_AWS_IDP_DOCUMENT_TYPE])
                        .map((cls) => (
                          <Container key={cls.id} disableContentPaddings={false}>
                            <div
                              data-schema-class-id={cls.id}
                              role="button"
                              tabIndex={0}
                              onClick={() => setSelectedClassId(cls.id)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' || e.key === ' ') {
                                  setSelectedClassId(cls.id);
                                }
                              }}
                              style={{
                                cursor: 'pointer',
                                padding: '12px',
                                borderRadius: '8px',
                                border: selectedClassId === cls.id ? '2px solid #0972d3' : '2px solid transparent',
                                backgroundColor: selectedClassId === cls.id ? '#e8f4fd' : 'transparent',
                                transition: 'all 0.2s ease',
                              }}
                            >
                              <SpaceBetween size="xs">
                                <Box>
                                  <SpaceBetween direction="horizontal" size="s" alignItems="center">
                                    <DocumentTypeBadge isRuleSchema={isRuleSchema} />
                                    <Box fontWeight="bold">{cls.name}</Box>
                                    <Box float="right">
                                      <SpaceBetween direction="horizontal" size="xs">
                                        <Button
                                          variant="icon"
                                          iconName="edit"
                                          onClick={(e) => {
                                            e.stopPropagation();
                                            handleEditClass(cls);
                                          }}
                                          ariaLabel={`Edit ${cls.name}`}
                                        />
                                        <Button
                                          variant="icon"
                                          iconName="close"
                                          onClick={(e) => {
                                            e.stopPropagation();
                                            setClassToDelete(cls);
                                            setShowDeleteConfirmModal(true);
                                          }}
                                          ariaLabel={`Delete ${cls.name}`}
                                        />
                                      </SpaceBetween>
                                    </Box>
                                  </SpaceBetween>
                                </Box>
                                {cls.description && (
                                  <Box fontSize="body-s" color="text-body-secondary">
                                    {cls.description}
                                  </Box>
                                )}
                                <Box fontSize="body-s" color="text-body-secondary">
                                  {Object.keys(cls.attributes?.properties || {}).length}{' '}
                                  {isRuleSchema ? attributesLabel.toLowerCase() : 'attribute(s)'}
                                </Box>
                              </SpaceBetween>
                            </div>
                          </Container>
                        ))}
                    </SpaceBetween>
                  </Box>

                  {!isRuleSchema && (
                    <Box>
                      <Header {...({ variant: 'h4' } as Record<string, unknown>)}>{sharedLabel}</Header>
                      <SpaceBetween size="s">
                        {classes.filter((c) => !c[X_AWS_IDP_DOCUMENT_TYPE]).length === 0 && (
                          <Box fontSize="body-s" color="text-body-secondary" padding="s">
                            No shared classes. Shared classes can be referenced by {typeLabel} types via $ref.
                          </Box>
                        )}
                        {classes
                          .filter((cls) => !cls[X_AWS_IDP_DOCUMENT_TYPE])
                          .map((cls) => (
                            <Container key={cls.id} disableContentPaddings={false}>
                              <div
                                data-schema-class-id={cls.id}
                                role="button"
                                tabIndex={0}
                                onClick={() => setSelectedClassId(cls.id)}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter' || e.key === ' ') {
                                    setSelectedClassId(cls.id);
                                  }
                                }}
                                style={{
                                  cursor: 'pointer',
                                  padding: '12px',
                                  borderRadius: '8px',
                                  border: selectedClassId === cls.id ? '2px solid #0972d3' : '2px solid transparent',
                                  backgroundColor: selectedClassId === cls.id ? '#e8f4fd' : 'transparent',
                                  transition: 'all 0.2s ease',
                                }}
                              >
                                <SpaceBetween size="xs">
                                  <Box>
                                    <SpaceBetween direction="horizontal" size="s" alignItems="center">
                                      <Box fontWeight="bold">{cls.name}</Box>
                                      <Box float="right">
                                        <SpaceBetween direction="horizontal" size="xs">
                                          <Button
                                            variant="icon"
                                            iconName="edit"
                                            onClick={(e) => {
                                              e.stopPropagation();
                                              handleEditClass(cls);
                                            }}
                                            ariaLabel={`Edit ${cls.name}`}
                                          />
                                          <Button
                                            variant="icon"
                                            iconName="close"
                                            onClick={(e) => {
                                              e.stopPropagation();
                                              setClassToDelete(cls);
                                              setShowDeleteConfirmModal(true);
                                            }}
                                            ariaLabel={`Delete ${cls.name}`}
                                          />
                                        </SpaceBetween>
                                      </Box>
                                    </SpaceBetween>
                                  </Box>
                                  {cls.description && (
                                    <Box fontSize="body-s" color="text-body-secondary">
                                      {cls.description}
                                    </Box>
                                  )}
                                  <Box fontSize="body-s" color="text-body-secondary">
                                    {Object.keys(cls.attributes?.properties || {}).length} attribute(s)
                                  </Box>
                                </SpaceBetween>
                              </div>
                            </Container>
                          ))}
                      </SpaceBetween>
                    </Box>
                  )}
                </SpaceBetween>
              </Box>

              {!showPreview && (
                <>
                  <SchemaCanvas
                    selectedClass={getSelectedClass()}
                    selectedAttributeId={selectedAttributeId}
                    onSelectAttribute={setSelectedAttributeId}
                    onUpdateAttribute={(name, updates) => {
                      if (selectedClassId) updateAttribute(selectedClassId, name, updates);
                    }}
                    onRemoveAttribute={(name) => {
                      if (selectedClassId) removeAttribute(selectedClassId, name);
                    }}
                    onReorder={(oldIndex, newIndex) => {
                      if (selectedClassId) reorderAttributes(selectedClassId, oldIndex, newIndex);
                    }}
                    onNavigateToClass={(classId) => {
                      setSelectedClassId(classId);
                      setSelectedAttributeId(null);
                    }}
                    onNavigateToAttribute={(classId, attributeName) => {
                      setSelectedClassId(classId);
                      setSelectedAttributeId(attributeName);
                    }}
                    onAddAttribute={handleAddAttribute}
                    availableClasses={classes}
                    isRuleSchema={isRuleSchema}
                  />

                  <SchemaInspector
                    selectedClass={getSelectedClass()}
                    selectedAttribute={getSelectedAttribute()}
                    selectedAttributeName={selectedAttributeId}
                    onUpdate={(updates) => {
                      if (selectedClassId && selectedAttributeId) updateAttribute(selectedClassId, selectedAttributeId, updates);
                    }}
                    onUpdateClass={(updates) => {
                      if (selectedClassId) updateClass(selectedClassId, updates);
                    }}
                    onRenameAttribute={(newName) => {
                      if (selectedClassId && selectedAttributeId) return renameAttribute(selectedClassId, selectedAttributeId, newName);
                      return false;
                    }}
                    availableClasses={classes}
                    isRequired={
                      (selectedAttributeId ? getSelectedClass()?.attributes?.required?.includes(selectedAttributeId) : false) || false
                    }
                    isRuleSchema={isRuleSchema}
                    onToggleRequired={(checked) => {
                      const selectedClass = getSelectedClass();
                      if (!selectedClass || !selectedClass.attributes) return;

                      const currentRequired = selectedClass.attributes.required || [];
                      let newRequired: string[];

                      if (!selectedAttributeId || !selectedClassId) return;
                      if (checked) {
                        if (!currentRequired.includes(selectedAttributeId)) {
                          newRequired = [...currentRequired, selectedAttributeId];
                        } else {
                          return; // Already required
                        }
                      } else {
                        newRequired = currentRequired.filter((name) => name !== selectedAttributeId);
                      }

                      // Only update the required field, not the entire attributes object
                      // This ensures immer properly tracks the change and creates new references
                      updateClass(selectedClassId, {
                        attributes: {
                          required: newRequired,
                        },
                      });
                    }}
                    onNavigateToClass={(classId) => {
                      setSelectedClassId(classId);
                      setSelectedAttributeId(null);
                    }}
                    onNavigateToAttribute={(classId, attributeName) => {
                      setSelectedClassId(classId);
                      setSelectedAttributeId(attributeName);
                    }}
                  />
                </>
              )}

              {showPreview && (
                <SchemaPreviewTabs
                  classes={classes}
                  selectedClassId={selectedClassId}
                  exportedSchemas={currentSchema}
                  onSelectClass={(classId) => {
                    setSelectedClassId(classId);
                    setSelectedAttributeId(null);
                  }}
                />
              )}
            </ColumnLayout>
          </Container>
        </div>

        <Modal
          visible={showAddClassModal}
          onDismiss={() => {
            setShowAddClassModal(false);
            setAddClassMode('choose');
            setNewClassName('');
            setNewClassDescription('');
          }}
          size={addClassMode === 'standard' ? 'large' : 'medium'}
          header={
            addClassMode === 'choose'
              ? 'Add Class'
              : addClassMode === 'standard'
                ? 'Import Standard Class'
                : isRuleSchema
                  ? 'Add Policy Class'
                  : 'Add Custom Class'
          }
          footer={
            addClassMode === 'custom' ? (
              <Box float="right">
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant="link"
                    onClick={() => {
                      if (!isRuleSchema) {
                        setAddClassMode('choose');
                        setNewClassName('');
                        setNewClassDescription('');
                      } else {
                        setShowAddClassModal(false);
                        setNewClassName('');
                        setNewClassDescription('');
                      }
                    }}
                  >
                    {isRuleSchema ? 'Cancel' : 'Back'}
                  </Button>
                  <Button variant="primary" onClick={handleConfirmAddClass} disabled={!isClassNameValid(newClassName)}>
                    {isRuleSchema ? 'Add Policy Class' : 'Add Class'}
                  </Button>
                </SpaceBetween>
              </Box>
            ) : addClassMode === 'choose' ? (
              <Box float="right">
                <Button
                  variant="link"
                  onClick={() => {
                    setShowAddClassModal(false);
                    setAddClassMode('choose');
                  }}
                >
                  Cancel
                </Button>
              </Box>
            ) : undefined /* standard mode has its own footer via StandardClassCatalog */
          }
        >
          {addClassMode === 'choose' && (
            <SpaceBetween size="l">
              <Box variant="p">How would you like to create your class?</Box>
              <ColumnLayout columns={2}>
                <div
                  role="button"
                  tabIndex={0}
                  onClick={() => setAddClassMode('custom')}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') setAddClassMode('custom');
                  }}
                  style={{
                    padding: '20px',
                    border: '2px solid #e9ebed',
                    borderRadius: '12px',
                    cursor: 'pointer',
                    textAlign: 'center',
                    transition: 'all 0.2s ease',
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = '#0972d3';
                    e.currentTarget.style.backgroundColor = '#f2f8fd';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = '#e9ebed';
                    e.currentTarget.style.backgroundColor = 'transparent';
                  }}
                >
                  <SpaceBetween size="s">
                    <Box fontSize="heading-m">📝</Box>
                    <Box fontWeight="bold" fontSize="body-m">
                      Custom Class
                    </Box>
                    <Box fontSize="body-s" color="text-body-secondary">
                      Define your own class with custom fields and attributes
                    </Box>
                  </SpaceBetween>
                </div>
                <div
                  role="button"
                  tabIndex={0}
                  onClick={() => setAddClassMode('standard')}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') setAddClassMode('standard');
                  }}
                  style={{
                    padding: '20px',
                    border: '2px solid #e9ebed',
                    borderRadius: '12px',
                    cursor: 'pointer',
                    textAlign: 'center',
                    transition: 'all 0.2s ease',
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = '#0972d3';
                    e.currentTarget.style.backgroundColor = '#f2f8fd';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = '#e9ebed';
                    e.currentTarget.style.backgroundColor = 'transparent';
                  }}
                >
                  <SpaceBetween size="s">
                    <Box fontSize="heading-m">📦</Box>
                    <Box fontWeight="bold" fontSize="body-m">
                      Standard Class
                    </Box>
                    <Box fontSize="body-s" color="text-body-secondary">
                      Choose from pre-built classes for common document types (Invoice, Receipt, etc.)
                    </Box>
                  </SpaceBetween>
                </div>
              </ColumnLayout>
            </SpaceBetween>
          )}

          {addClassMode === 'custom' && (
            <SpaceBetween size="m">
              <FormField
                label={isRuleSchema ? 'Policy Class Name' : 'Class Name'}
                description={`A unique name for this ${isRuleSchema ? 'rule' : 'extraction'} class`}
                errorText={classNameErrorText}
              >
                <Input
                  value={newClassName}
                  onChange={({ detail }) => setNewClassName(detail.value)}
                  placeholder={isRuleSchema ? 'e.g., ComplianceRules, SafetyChecks' : 'e.g., Invoice, Customer, Address'}
                />
              </FormField>
              <FormField label="Description (Optional)" description="Describe what this class represents">
                <Textarea
                  value={newClassDescription}
                  onChange={({ detail }) => setNewClassDescription(detail.value)}
                  placeholder="e.g., Invoice document with line items"
                  rows={3}
                />
              </FormField>
            </SpaceBetween>
          )}

          {addClassMode === 'standard' && (
            <StandardClassCatalog
              onImport={(schemas) => {
                addStandardClasses(schemas as Record<string, unknown>[]);
                setShowAddClassModal(false);
                setAddClassMode('choose');
              }}
              existingClassNames={classes.map((c) => c.name)}
              onCancel={() => {
                setAddClassMode('choose');
              }}
            />
          )}
        </Modal>

        <Modal
          visible={showAddAttributeModal}
          onDismiss={() => {
            setShowAddAttributeModal(false);
            setNewAttributeName('');
            setNewAttributeType({ label: 'String', value: 'string' });
            setNewAttributeDescription('');
            setNewAttributeReferenceClass(null);
          }}
          header={isRuleSchema ? 'Add Rule' : 'Add Attribute'}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={() => {
                    setShowAddAttributeModal(false);
                    setNewAttributeName('');
                    setNewAttributeType({ label: 'String', value: 'string' });
                    setNewAttributeDescription('');
                    setNewAttributeReferenceClass(null);
                  }}
                >
                  Cancel
                </Button>
                <Button variant="normal" onClick={() => handleConfirmAddAttribute(true)} disabled={!newAttributeName.trim()}>
                  Add another
                </Button>
                <Button variant="primary" onClick={() => handleConfirmAddAttribute(false)} disabled={!newAttributeName.trim()}>
                  {isRuleSchema ? 'Add Rule' : 'Add Attribute'}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <FormField
              label={isRuleSchema ? 'Rule Name' : 'Attribute Name'}
              description={isRuleSchema ? 'The rule name' : 'The field name to extract from documents'}
            >
              <Input
                ref={addAttributeNameRef}
                value={newAttributeName}
                onChange={({ detail }) => setNewAttributeName(detail.value)}
                placeholder={isRuleSchema ? 'e.g., checkCompliance, validateSafety' : 'e.g., invoiceNumber, customerName, total'}
              />
            </FormField>
            <FormField
              label={isRuleSchema ? 'Rule Output Data Type' : 'Attribute Type'}
              description={isRuleSchema ? 'The type of data this rule validates' : 'The type of data this field contains'}
            >
              <Select
                selectedOption={newAttributeType}
                onChange={({ detail }) => setNewAttributeType(detail.selectedOption as TypeOption)}
                options={attributeTypeOptions}
              />
            </FormField>
            <FormField label="Description (Optional)" description="Describe what this field contains">
              <Textarea
                value={newAttributeDescription}
                onChange={({ detail }) => setNewAttributeDescription(detail.value)}
                placeholder="e.g., The unique invoice number for this document"
                rows={3}
              />
            </FormField>

            {(newAttributeType.value === 'object' || newAttributeType.value === 'array') && classes.length > 1 && (
              <FormField
                label={newAttributeType.value === 'object' ? 'Object Type' : 'Array Item Type'}
                description={
                  newAttributeType.value === 'object'
                    ? 'Define the structure: reference an existing class or create inline properties'
                    : 'What type of items will this array contain? Reference a class to create a list of ' +
                      'complex objects (e.g., list of persons)'
                }
                info={
                  newAttributeType.value === 'array' ? (
                    <Box>Example: If you have a &quot;person&quot; class, select it here to create an array of person objects</Box>
                  ) : undefined
                }
              >
                <Select
                  selectedOption={newAttributeReferenceClass}
                  onChange={({ detail }) => setNewAttributeReferenceClass(detail.selectedOption as TypeOption)}
                  options={[
                    {
                      label: newAttributeType.value === 'object' ? '⚙️ Inline properties' : '📝 Simple values (string)',
                      value: '',
                      description:
                        newAttributeType.value === 'object' ? 'Define properties directly in this object' : 'Array of simple strings',
                    },
                    ...classes
                      .filter((cls) => cls.id !== selectedClassId)
                      .map((cls) => ({
                        label: `🔗 ${cls.name}`,
                        value: cls.name,
                        description: cls.description || `Reference the ${cls.name} class`,
                      })),
                  ]}
                  placeholder={
                    newAttributeType.value === 'array'
                      ? 'e.g., select "person" to create list of persons'
                      : 'Select a class or define inline'
                  }
                />
              </FormField>
            )}
          </SpaceBetween>
        </Modal>

        <Modal
          visible={showEditClassModal}
          onDismiss={() => {
            setShowEditClassModal(false);
            setEditingClass(null);
            setNewClassName('');
            setNewClassDescription('');
          }}
          header="Edit Class"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={() => {
                    setShowEditClassModal(false);
                    setEditingClass(null);
                    setNewClassName('');
                    setNewClassDescription('');
                  }}
                >
                  Cancel
                </Button>
                <Button variant="primary" onClick={handleConfirmEditClass} disabled={!isClassNameValid(newClassName)}>
                  Save Changes
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <FormField label="Class Name" description="A unique name for this extraction class" errorText={classNameErrorText}>
              <Input
                value={newClassName}
                onChange={({ detail }) => setNewClassName(detail.value)}
                placeholder="e.g., Invoice, Customer, Address"
              />
            </FormField>
            <FormField label="Description (Optional)" description="Describe what this class represents">
              <Textarea
                value={newClassDescription}
                onChange={({ detail }) => setNewClassDescription(detail.value)}
                placeholder="e.g., Invoice document with line items"
                rows={3}
              />
            </FormField>
          </SpaceBetween>
        </Modal>

        <Modal
          visible={showDeleteConfirmModal}
          onDismiss={() => {
            setShowDeleteConfirmModal(false);
            setClassToDelete(null);
          }}
          header="Delete Class"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={() => {
                    setShowDeleteConfirmModal(false);
                    setClassToDelete(null);
                  }}
                >
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={() => {
                    if (classToDelete) {
                      removeClass(classToDelete.id);
                    }
                    setShowDeleteConfirmModal(false);
                    setClassToDelete(null);
                  }}
                >
                  Delete
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <Alert type="warning">
              Are you sure you want to delete the class <strong>{classToDelete?.name}</strong>? This action cannot be undone.
            </Alert>
            {classToDelete && Object.keys(classToDelete.attributes?.properties || {}).length > 0 && (
              <Alert type="info">
                This class has {Object.keys(classToDelete.attributes!.properties!).length} attribute(s) that will also be deleted.
              </Alert>
            )}
          </SpaceBetween>
        </Modal>

        <Modal
          visible={showWipeAllModal}
          onDismiss={() => setShowWipeAllModal(false)}
          header={`Wipe All ${TypeLabel} Classes`}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setShowWipeAllModal(false)}>
                  Cancel
                </Button>
                <Button variant="primary" onClick={handleConfirmWipeAll}>
                  Wipe All
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <Alert type="error">
              Are you sure you want to delete{' '}
              <strong>
                all {classes.length} {typeLabel} class(es)
              </strong>
              ? This action cannot be undone.
            </Alert>
            <Box variant="p">
              All {typeLabel} classes and their attributes will be permanently removed.
              {!isRuleSchema && ' You will need to recreate them or use the discovery feature to regenerate your schema.'}
            </Box>
          </SpaceBetween>
        </Modal>
      </SpaceBetween>
    </>
  );
};

export default SchemaBuilder;
