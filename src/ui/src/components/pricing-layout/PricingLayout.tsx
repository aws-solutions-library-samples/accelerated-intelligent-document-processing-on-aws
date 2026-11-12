// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  Button,
  Alert,
  Spinner,
  Form,
  SegmentedControl,
  ExpandableSection,
  Table,
  Input,
  Modal,
  FormField,
  RadioGroup,
} from '@cloudscape-design/components';
import Editor from '@monaco-editor/react';

import yaml from 'js-yaml';
import usePricing from '../../hooks/use-pricing';
import useUserRole from '../../hooks/use-user-role';
import { hasPricingCustomizations } from './pricing-customizations';

interface PricingUnit {
  name: string;
  price: string;
}

interface PricingEntry {
  name: string;
  units: PricingUnit[];
}

interface PricingFormValues {
  pricing: PricingEntry[];
}

interface ValidationError {
  message: string;
}

interface PricingTableItem {
  apiName: string;
  displayName: string;
  unitName: string;
  price: string;
}

const PricingLayout = (): React.JSX.Element => {
  const { pricing, defaultPricing, loading, refreshing, error, updatePricing, fetchPricing, restoreDefaultPricing } = usePricing();
  const { isAdmin } = useUserRole();

  const [formValues, setFormValues] = useState<PricingFormValues>({ pricing: [] });
  const [jsonContent, setJsonContent] = useState('');
  const [yamlContent, setYamlContent] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [validationErrors, setValidationErrors] = useState<ValidationError[]>([]);
  const [viewMode, setViewMode] = useState('form');
  const [showExportModal, setShowExportModal] = useState(false);
  const [exportFormat, setExportFormat] = useState('json');
  const [exportFileName, setExportFileName] = useState('pricing');
  const [importError, setImportError] = useState<string | null>(null);
  const [showRestoreModal, setShowRestoreModal] = useState(false);
  const [isRestoring, setIsRestoring] = useState(false);
  const [showAddServiceModal, setShowAddServiceModal] = useState(false);
  const [newServiceName, setNewServiceName] = useState('');
  const [newServiceUnits, setNewServiceUnits] = useState<{ name: string; price: string }[]>([{ name: '', price: '' }]);

  // Service display names mapping
  const serviceDisplayNames: Record<string, string> = {
    textract: 'Amazon Textract',
    bedrock: 'Amazon Bedrock',
    bda: 'Amazon BDA',
    sagemaker: 'Amazon SageMaker',
    lambda: 'AWS Lambda',
  };

  // Service display order (Amazon services first, then AWS Lambda last, then alphabetically)
  const serviceDisplayOrder: Record<string, number> = {
    textract: 1,
    bedrock: 2,
    bda: 3,
    sagemaker: 4,
    lambda: 5,
  };

  // Get display name for a service
  const getServiceDisplayName = (service: string): string => {
    return serviceDisplayNames[service] || service.charAt(0).toUpperCase() + service.slice(1);
  };

  // Group pricing entries by service (extracted from name before "/")
  const groupPricingByService = (pricingArray: PricingEntry[]): Record<string, PricingEntry[]> => {
    if (!Array.isArray(pricingArray)) return {};

    const grouped: Record<string, PricingEntry[]> = {};
    pricingArray.forEach((entry) => {
      if (entry.name && entry.name.includes('/')) {
        const [service] = entry.name.split('/');
        if (!grouped[service]) {
          grouped[service] = [];
        }
        grouped[service].push(entry);
      }
    });
    return grouped;
  };

  // Initialize form values from pricing
  useEffect(() => {
    if (pricing) {
      console.log('Setting form values from pricing:', pricing);
      const formData = JSON.parse(JSON.stringify(pricing));
      setFormValues(formData);

      const jsonString = JSON.stringify(pricing, null, 2);
      setJsonContent(jsonString);

      try {
        const yamlString = yaml.dump(pricing);
        setYamlContent(yamlString);
      } catch (e) {
        console.error('Error converting to YAML:', e);
        setYamlContent('# Error converting to YAML');
      }
    }
  }, [pricing]);

  // Check if the table differs from the defaults (see pricing-customizations.ts)
  const hasCustomizations = (): boolean => hasPricingCustomizations(formValues, defaultPricing);

  // Handle changes in the JSON editor
  const handleJsonEditorChange = (value: string | undefined): void => {
    setJsonContent(value ?? '');
    try {
      const parsedValue = JSON.parse(value ?? '');
      setFormValues(parsedValue);

      try {
        const yamlString = yaml.dump(parsedValue);
        setYamlContent(yamlString);
      } catch (yamlErr) {
        console.error('Error converting to YAML:', yamlErr);
      }

      setValidationErrors([]);
    } catch (e) {
      setValidationErrors([{ message: `Invalid JSON: ${(e as Error).message}` }]);
    }
  };

  // Handle changes in the YAML editor
  const handleYamlEditorChange = (value: string | undefined): void => {
    setYamlContent(value ?? '');
    try {
      const parsedValue = yaml.load(value ?? '') as PricingFormValues;
      setFormValues(parsedValue);

      try {
        const jsonString = JSON.stringify(parsedValue, null, 2);
        setJsonContent(jsonString);
      } catch (jsonErr) {
        console.error('Error converting to JSON:', jsonErr);
      }

      setValidationErrors([]);
    } catch (e) {
      setValidationErrors([{ message: `Invalid YAML: ${(e as Error).message}` }]);
    }
  };

  const handleSave = async () => {
    if (validationErrors.length > 0) {
      setSaveError('Cannot save: Pricing contains validation errors');
      return;
    }

    setIsSaving(true);
    setSaveSuccess(false);
    setSaveError(null);

    try {
      const success = await updatePricing(formValues);

      if (success) {
        setSaveSuccess(true);
        setTimeout(() => setSaveSuccess(false), 3000);
      } else {
        setSaveError('Failed to save pricing. Please try again.');
      }
    } catch (err) {
      console.error('Save error:', err);
      setSaveError(`Error: ${(err as Error).message}`);
    } finally {
      setIsSaving(false);
    }
  };

  const handleRestoreAllDefaults = async () => {
    setIsRestoring(true);
    setSaveSuccess(false);
    setSaveError(null);

    try {
      const success = await restoreDefaultPricing();

      if (success) {
        setSaveSuccess(true);
        setShowRestoreModal(false);
        setTimeout(() => setSaveSuccess(false), 3000);
      } else {
        setSaveError('Failed to restore default pricing. Please try again.');
      }
    } catch (err) {
      console.error('Restore error:', err);
      setSaveError(`Error: ${(err as Error).message}`);
    } finally {
      setIsRestoring(false);
    }
  };

  const handleAddService = () => {
    if (!newServiceName.trim() || newServiceUnits.length === 0) {
      return;
    }

    const newFormValues = JSON.parse(JSON.stringify(formValues));
    if (!newFormValues.pricing) {
      newFormValues.pricing = [];
    }

    // Build units array from dynamic entries
    const units = newServiceUnits
      .filter((unit) => unit.name.trim() && unit.price.trim())
      .map((unit) => ({
        name: unit.name.trim(),
        price: unit.price.trim(),
      }));

    if (units.length > 0) {
      newFormValues.pricing.push({
        name: newServiceName.trim(),
        units,
      });

      setFormValues(newFormValues);
      setJsonContent(JSON.stringify(newFormValues, null, 2));
      try {
        setYamlContent(yaml.dump(newFormValues));
      } catch (e) {
        console.error('Error converting to YAML:', e);
      }
    }

    // Reset modal state
    setShowAddServiceModal(false);
    setNewServiceName('');
    setNewServiceUnits([{ name: '', price: '' }]);
  };

  // Helper functions for managing dynamic units in Add Service modal
  const handleAddUnit = () => {
    setNewServiceUnits([...newServiceUnits, { name: '', price: '' }]);
  };

  const handleRemoveUnit = (index: number): void => {
    if (newServiceUnits.length > 1) {
      setNewServiceUnits(newServiceUnits.filter((_, i) => i !== index));
    }
  };

  const handleUpdateUnit = (index: number, field: 'name' | 'price', value: string): void => {
    const updated = [...newServiceUnits];
    updated[index][field] = value;
    setNewServiceUnits(updated);
  };

  const handleExport = () => {
    try {
      let content;
      let mimeType;
      let fileExtension;

      if (exportFormat === 'yaml') {
        content = yaml.dump(formValues);
        mimeType = 'text/yaml';
        fileExtension = 'yaml';
      } else {
        content = JSON.stringify(formValues, null, 2);
        mimeType = 'application/json';
        fileExtension = 'json';
      }

      const blob = new Blob([content], { type: mimeType });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${exportFileName}.${fileExtension}`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
      setShowExportModal(false);
    } catch (err) {
      setSaveError(`Export failed: ${(err as Error).message}`);
    }
  };

  const handleImport = (event: React.ChangeEvent<HTMLInputElement>): void => {
    const file = event.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (e: ProgressEvent<FileReader>) => {
      try {
        setImportError(null);
        const content = e.target?.result as string;

        const importedPricing = file.name.endsWith('.yaml') || file.name.endsWith('.yml') ? yaml.load(content) : JSON.parse(content);

        if (importedPricing && typeof importedPricing === 'object') {
          setFormValues(importedPricing);
          setJsonContent(JSON.stringify(importedPricing, null, 2));
          try {
            setYamlContent(yaml.dump(importedPricing));
          } catch (yamlErr) {
            console.error('Error converting to YAML:', yamlErr);
          }
          setSaveSuccess(false);
          setSaveError(null);
        } else {
          setImportError('Invalid pricing file format');
        }
      } catch (err) {
        setImportError(`Import failed: ${(err as Error).message}`);
      }
    };
    reader.readAsText(file);
    event.target.value = '';
  };

  // Update a specific unit price
  const updateUnitPrice = (apiName: string, unitName: string, newPrice: string): void => {
    const newFormValues = JSON.parse(JSON.stringify(formValues));
    const entry = newFormValues.pricing.find((e: PricingEntry) => e.name === apiName);
    if (entry && entry.units) {
      const unit = entry.units.find((u: PricingUnit) => u.name === unitName);
      if (unit) {
        unit.price = newPrice;
        setFormValues(newFormValues);
        setJsonContent(JSON.stringify(newFormValues, null, 2));
        try {
          setYamlContent(yaml.dump(newFormValues));
        } catch (e) {
          console.error('Error converting to YAML:', e);
        }
      }
    }
  };

  // Delete a specific unit
  const handleDeleteUnit = (apiName: string, unitName: string): void => {
    const newFormValues = JSON.parse(JSON.stringify(formValues));
    const entry = newFormValues.pricing.find((e: PricingEntry) => e.name === apiName);
    if (entry && entry.units) {
      entry.units = entry.units.filter((u: PricingUnit) => u.name !== unitName);
      // Remove entry if no units remain
      if (entry.units.length === 0) {
        newFormValues.pricing = newFormValues.pricing.filter((e: PricingEntry) => e.name !== apiName);
      }
      setFormValues(newFormValues);
      setJsonContent(JSON.stringify(newFormValues, null, 2));
      try {
        setYamlContent(yaml.dump(newFormValues));
      } catch (e) {
        console.error('Error converting to YAML:', e);
      }
    }
  };

  // Delete an entire API/service entry
  const _handleDeleteService = (apiName: string): void => {
    const newFormValues = JSON.parse(JSON.stringify(formValues));
    newFormValues.pricing = newFormValues.pricing.filter((e: PricingEntry) => e.name !== apiName);
    setFormValues(newFormValues);
    setJsonContent(JSON.stringify(newFormValues, null, 2));
    try {
      setYamlContent(yaml.dump(newFormValues));
    } catch (e) {
      console.error('Error converting to YAML:', e);
    }
  };

  // Render pricing table for a service
  const renderServiceTable = (serviceEntries: PricingEntry[]): React.JSX.Element | null => {
    if (!serviceEntries || serviceEntries.length === 0) {
      return <Box color="text-status-inactive">No pricing data configured</Box>;
    }

    // Flatten entries into rows: API name + each unit as a row
    const items: PricingTableItem[] = [];
    serviceEntries.forEach((entry) => {
      if (entry.units && Array.isArray(entry.units)) {
        entry.units.forEach((unit) => {
          items.push({
            apiName: entry.name,
            displayName: entry.name.includes('/') ? entry.name.split('/').slice(1).join('/') : entry.name,
            unitName: unit.name,
            price: unit.price,
          });
        });
      }
    });

    if (items.length === 0) return null;

    return (
      <Table
        columnDefinitions={[
          {
            id: 'api',
            header: 'Service / API',
            cell: (item) => <span>{item.displayName}</span>,
            width: 400,
          },
          {
            id: 'unit',
            header: 'Unit',
            cell: (item) => <span>{item.unitName}</span>,
            width: 180,
          },
          {
            id: 'price',
            header: 'Price ($)',
            cell: (item) => (
              <Input
                type="text"
                value={String(item.price)}
                onChange={({ detail }) => updateUnitPrice(item.apiName, item.unitName, detail.value)}
                disabled={!isAdmin}
                readOnly={!isAdmin}
              />
            ),
            width: 200,
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: (item) => (
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="icon"
                  iconName="remove"
                  onClick={() => handleDeleteUnit(item.apiName, item.unitName)}
                  disabled={!isAdmin}
                  ariaLabel="Delete unit"
                />
              </SpaceBetween>
            ),
            width: 100,
          },
        ]}
        items={items}
        variant="embedded"
        stripedRows
        sortingDisabled
      />
    );
  };

  // Render a service section
  const renderServiceSection = (service: string, serviceEntries: PricingEntry[]): React.JSX.Element => {
    return (
      <ExpandableSection headerText={`${getServiceDisplayName(service)} Pricing`} defaultExpanded={false} key={service}>
        <Box padding="s">{renderServiceTable(serviceEntries)}</Box>
      </ExpandableSection>
    );
  };

  if (loading) {
    return (
      <Container header={<Header variant="h2">Pricing</Header>}>
        <Box textAlign="center" padding="l">
          <Spinner size="large" />
          <Box padding="s">Loading pricing data...</Box>
        </Box>
      </Container>
    );
  }

  if (error) {
    return (
      <Container header={<Header variant="h2">Pricing</Header>}>
        <Alert type="error" header="Error loading pricing">
          <SpaceBetween size="s">
            <div>{error}</div>
            <Box>
              <Button {...({ onClick: fetchPricing } as Record<string, unknown>)} variant="primary">
                Retry
              </Button>
            </Box>
          </SpaceBetween>
        </Alert>
      </Container>
    );
  }

  if (!pricing) {
    return (
      <Container header={<Header variant="h2">Pricing</Header>}>
        <Alert type="error" header="Pricing not available">
          <SpaceBetween size="s">
            <div>Unable to load pricing data.</div>
            <Box>
              <Button {...({ onClick: fetchPricing } as Record<string, unknown>)} variant="primary">
                Retry
              </Button>
            </Box>
          </SpaceBetween>
        </Alert>
      </Container>
    );
  }

  const groupedPricing = groupPricingByService(formValues.pricing || []);
  const services = Object.keys(groupedPricing).sort((a, b) => {
    const orderA = serviceDisplayOrder[a] || 999;
    const orderB = serviceDisplayOrder[b] || 999;
    if (orderA !== orderB) {
      return orderA - orderB;
    }
    return a.localeCompare(b);
  });

  return (
    <>
      {/* Add Service/API Modal */}
      <Modal
        visible={showAddServiceModal}
        onDismiss={() => {
          setShowAddServiceModal(false);
          setNewServiceName('');
          setNewServiceUnits([{ name: '', price: '' }]);
        }}
        header="Add Service/API Pricing"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowAddServiceModal(false);
                  setNewServiceName('');
                  setNewServiceUnits([{ name: '', price: '' }]);
                }}
              >
                Cancel
              </Button>
              <Button variant="primary" onClick={handleAddService} disabled={!newServiceName.trim() || newServiceUnits.length === 0}>
                Add
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween direction="vertical" size="l">
          <FormField
            label="Service/API Name"
            description="Enter the full path (e.g., 'textract/detect_document_text' or 'bedrock/us.amazon.nova-lite-v1:0')"
          >
            <Input value={newServiceName} onChange={({ detail }) => setNewServiceName(detail.value)} placeholder="service/api-name" />
          </FormField>

          <FormField label="Pricing Units" description="Add unit types and their prices">
            <SpaceBetween direction="vertical" size="s">
              {newServiceUnits.map((unit, index) => (
                // eslint-disable-next-line react/no-array-index-key
                <SpaceBetween key={index} direction="horizontal" size="xs">
                  <Input
                    value={unit.name}
                    onChange={({ detail }) => handleUpdateUnit(index, 'name', detail.value)}
                    placeholder="Unit name (e.g., pages, inputTokens)"
                    ariaLabel="Unit name"
                  />
                  <Input
                    type="text"
                    value={unit.price}
                    onChange={({ detail }) => handleUpdateUnit(index, 'price', detail.value)}
                    placeholder="Price (e.g., 0.0015, 6.0E-8)"
                    ariaLabel="Unit price"
                  />
                  <Button
                    variant="icon"
                    iconName="remove"
                    onClick={() => handleRemoveUnit(index)}
                    disabled={newServiceUnits.length <= 1}
                    ariaLabel="Remove unit"
                  />
                </SpaceBetween>
              ))}
              <Button variant="normal" iconName="add-plus" onClick={handleAddUnit}>
                Add unit
              </Button>
            </SpaceBetween>
          </FormField>
        </SpaceBetween>
      </Modal>

      {/* Restore All Defaults Confirmation Modal */}
      <Modal
        visible={showRestoreModal}
        onDismiss={() => setShowRestoreModal(false)}
        header="Restore All Pricing to Default"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowRestoreModal(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleRestoreAllDefaults} loading={isRestoring}>
                Restore Defaults
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Box variant="span">
          Are you sure you want to restore all pricing values to their default settings? This will discard all custom pricing changes.
        </Box>
      </Modal>

      <Modal
        visible={showExportModal}
        onDismiss={() => setShowExportModal(false)}
        header="Export Pricing"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowExportModal(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleExport}>
                Export
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween direction="vertical" size="l">
          <FormField label="File format">
            <RadioGroup
              value={exportFormat}
              onChange={({ detail }) => setExportFormat(detail.value)}
              items={[
                { value: 'json', label: 'JSON' },
                { value: 'yaml', label: 'YAML' },
              ]}
            />
          </FormField>
          <FormField label="File name">
            <Input value={exportFileName} onChange={({ detail }) => setExportFileName(detail.value)} placeholder="pricing" />
          </FormField>
        </SpaceBetween>
      </Modal>

      <Container
        header={
          <Header
            variant="h2"
            description={
              <>
                Configure pricing for AWS services used in document processing. <strong>Note:</strong> These are estimated prices and may be
                outdated. Always verify current pricing for the relevant service (e.g.,{' '}
                <a href="https://aws.amazon.com/bedrock/pricing/" target="_blank" rel="noopener noreferrer">
                  AWS Bedrock Pricing
                </a>
                ,{' '}
                <a href="https://aws.amazon.com/textract/pricing/" target="_blank" rel="noopener noreferrer">
                  AWS Textract Pricing
                </a>
                ).
              </>
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <SegmentedControl
                  selectedId={viewMode}
                  onChange={({ detail }) => setViewMode(detail.selectedId)}
                  options={[
                    { id: 'form', text: 'Form View' },
                    { id: 'json', text: 'JSON View' },
                    { id: 'yaml', text: 'YAML View' },
                  ]}
                />
                <Button variant="normal" onClick={() => setShowExportModal(true)}>
                  Export
                </Button>
                {isAdmin && (
                  <>
                    <Button variant="normal" onClick={() => document.getElementById('import-pricing-file')?.click()}>
                      Import
                    </Button>
                    <input
                      id="import-pricing-file"
                      type="file"
                      accept=".json,.yaml,.yml"
                      style={{ display: 'none' }}
                      onChange={handleImport}
                    />
                    <Button variant="normal" onClick={() => setShowAddServiceModal(true)}>
                      Add Service/API
                    </Button>
                    <Button variant="normal" onClick={() => setShowRestoreModal(true)} disabled={!hasCustomizations()}>
                      Restore default (All)
                    </Button>
                    <Button variant="primary" onClick={handleSave} loading={isSaving}>
                      Save changes
                    </Button>
                  </>
                )}
              </SpaceBetween>
            }
          >
            {isAdmin ? 'Pricing Configuration' : 'View Pricing'}
          </Header>
        }
      >
        <Form>
          {refreshing && (
            <Alert type="info" header="Syncing pricing...">
              <Box {...({ display: 'flex', alignItems: 'center' } as Record<string, unknown>)}>
                <Spinner {...({ size: 'normal' } as Record<string, unknown>)} />
                <Box margin={{ left: 's' }}>Refreshing data from server</Box>
              </Box>
            </Alert>
          )}

          {saveSuccess && (
            <Alert type="success" dismissible onDismiss={() => setSaveSuccess(false)} header="Pricing saved successfully">
              Your pricing changes have been saved.
            </Alert>
          )}

          {saveError && (
            <Alert type="error" dismissible onDismiss={() => setSaveError(null)} header="Error saving pricing">
              {saveError}
            </Alert>
          )}

          {importError && (
            <Alert type="error" dismissible onDismiss={() => setImportError(null)} header="Import error">
              {importError}
            </Alert>
          )}

          {validationErrors.length > 0 && (
            <Alert type="warning" header="Validation errors">
              <ul>
                {validationErrors.map((e, index) => (
                  // eslint-disable-next-line react/no-array-index-key
                  <li key={index}>{e.message}</li>
                ))}
              </ul>
            </Alert>
          )}

          <Box padding="s">
            {viewMode === 'form' && (
              <SpaceBetween size="l">
                {services.length === 0 ? (
                  <Alert type="info" header="No pricing data configured">
                    <SpaceBetween size="s">
                      <Box>No pricing data has been loaded. Click &quot;Add Service/API&quot; to add pricing entries manually.</Box>
                    </SpaceBetween>
                  </Alert>
                ) : (
                  services.map((service) => renderServiceSection(service, groupedPricing[service]))
                )}
              </SpaceBetween>
            )}

            {viewMode === 'json' && (
              <Editor
                height="70vh"
                defaultLanguage="json"
                value={jsonContent}
                onChange={isAdmin ? handleJsonEditorChange : undefined}
                options={{
                  minimap: { enabled: false },
                  formatOnPaste: true,
                  formatOnType: true,
                  automaticLayout: true,
                  scrollBeyondLastLine: false,
                  folding: true,
                  lineNumbers: 'on',
                  renderLineHighlight: 'all',
                  tabSize: 2,
                  readOnly: !isAdmin,
                }}
              />
            )}

            {viewMode === 'yaml' && (
              <Box>
                <Editor
                  height="70vh"
                  defaultLanguage="yaml"
                  value={yamlContent}
                  onChange={isAdmin ? handleYamlEditorChange : undefined}
                  options={{
                    minimap: { enabled: false },
                    formatOnPaste: true,
                    formatOnType: true,
                    automaticLayout: true,
                    scrollBeyondLastLine: false,
                    folding: true,
                    lineNumbers: 'on',
                    renderLineHighlight: 'all',
                    tabSize: 2,
                    readOnly: !isAdmin,
                  }}
                />
              </Box>
            )}
          </Box>
        </Form>
      </Container>
    </>
  );
};

export default PricingLayout;
