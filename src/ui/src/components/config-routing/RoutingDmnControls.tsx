// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Export the routing table as a DMN file, and import one. The server does both
// conversions; an imported table replaces the one in the form, unsaved, after the
// Admin confirms.
//
// The tab's "More actions" menu starts an import and an export through the ref. This
// component holds what they need on the page: the file picker, the confirmation of an
// import, and the error of either.
import React, { useImperativeHandle, useRef, useState } from 'react';
import { Alert, Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

import { extractGraphQLErrorMessage } from '../../hooks/utils/graphql-error';
import { exportToTextFile } from '../common/download-func';
import { routingFromDmn, routingToDmn, type DmnVersion } from './routing-api';
import { tableOf, type RoutingConfig } from './routing-types';

/** The largest DMN file the server reads. Checked here so a larger one is not uploaded. */
export const MAX_DMN_BYTES = 512 * 1024;

const messages = defineMessages<{
  importAria: NoMessageValues;
  exportError: NoMessageValues;
  importError: NoMessageValues;
  tooLarge: { readonly limit: number };
  confirmHeader: NoMessageValues;
  confirmBody: { readonly rules: number; readonly file: string };
  confirmNote: NoMessageValues;
  cancel: NoMessageValues;
  replace: NoMessageValues;
}>({
  importAria: {
    id: 'routing.dmn.importAria',
    defaultMessage: 'DMN file to import',
    description: 'Accessible name of the file picker that reads a routing table from a DMN file.',
  },
  exportError: {
    id: 'routing.dmn.exportError',
    defaultMessage: 'The table could not be exported',
    description: 'Heading of the error shown when the DMN export fails.',
  },
  importError: {
    id: 'routing.dmn.importError',
    defaultMessage: 'The file could not be imported',
    description: 'Heading of the error shown when a DMN file cannot be read as a routing table.',
  },
  tooLarge: {
    id: 'routing.dmn.tooLarge',
    defaultMessage: 'The file is larger than {limit, number} KB.',
    description: 'Error for a DMN file that is too large to import. {limit} is the largest size in kilobytes.',
  },
  confirmHeader: {
    id: 'routing.dmn.confirmHeader',
    defaultMessage: 'Replace the routing table?',
    description: 'Title of the dialog that confirms replacing the routing table with an imported one.',
  },
  confirmBody: {
    id: 'routing.dmn.confirmBody',
    defaultMessage:
      '{file} holds {rules, plural, one {# routing rule} other {# routing rules}}. They replace every routing rule in the table.',
    description:
      'Text of the dialog that confirms an import. {file} is the name of the DMN file, {rules} the number of routing rules in it.',
  },
  confirmNote: {
    id: 'routing.dmn.confirmNote',
    defaultMessage: 'Nothing is saved until you save the configuration.',
    description: 'Second sentence of the dialog that confirms an import of a routing table.',
  },
  cancel: {
    id: 'routing.dmn.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the import confirmation without changing the table.',
  },
  replace: {
    id: 'routing.dmn.replace',
    defaultMessage: 'Replace',
    description: 'Button that replaces the routing table with the imported one.',
  },
});

/** What the tab's menu asks of the DMN controls. */
export interface RoutingDmnControlsRef {
  /** Opens the file picker. The file chosen replaces the table once the Admin confirms. */
  importDmn: () => void;
  /** Downloads the table as a DMN file of that version. */
  exportDmn: (version: DmnVersion) => void;
}

interface RoutingDmnControlsProps {
  routing: RoutingConfig;
  /** The profile being edited; it names the exported file. */
  profileName: string;
  /** Called with the imported table once the Admin confirms. */
  onImport: (routing: RoutingConfig) => void;
  /** Told when a conversion starts and ends, so the menu can show that it is busy. */
  onBusyChange?: (busy: boolean) => void;
}

const RoutingDmnControls = React.forwardRef<RoutingDmnControlsRef, RoutingDmnControlsProps>(
  ({ routing, profileName, onImport, onBusyChange }, ref): React.JSX.Element => {
    const intl = useIntl();
    const fileInput = useRef<HTMLInputElement>(null);
    const [error, setError] = useState<{ header: string; text: string } | null>(null);
    const [pending, setPending] = useState<{ file: string; routing: RoutingConfig } | null>(null);

    const exportDmn = async (version: DmnVersion): Promise<void> => {
      onBusyChange?.(true);
      setError(null);
      try {
        const xml = await routingToDmn(routing, version);
        await exportToTextFile(xml, `routing-${profileName}`, 'dmn', 'application/xml');
      } catch (err) {
        setError({ header: intl.formatMessage(messages.exportError), text: extractGraphQLErrorMessage(err) });
      } finally {
        onBusyChange?.(false);
      }
    };

    const importFile = async (file: File): Promise<void> => {
      setError(null);
      if (file.size > MAX_DMN_BYTES) {
        setError({
          header: intl.formatMessage(messages.importError),
          text: intl.formatMessage(messages.tooLarge, { limit: MAX_DMN_BYTES / 1024 }),
        });
        return;
      }
      onBusyChange?.(true);
      try {
        setPending({ file: file.name, routing: await routingFromDmn(await file.text()) });
      } catch (err) {
        // The server names the construct it could not read; show its message as it is.
        setError({ header: intl.formatMessage(messages.importError), text: extractGraphQLErrorMessage(err) });
      } finally {
        onBusyChange?.(false);
      }
    };

    useImperativeHandle(ref, () => ({
      importDmn: () => fileInput.current?.click(),
      exportDmn: (version) => void exportDmn(version),
    }));

    return (
      <>
        <input
          ref={fileInput}
          type="file"
          accept=".dmn,.xml"
          aria-label={intl.formatMessage(messages.importAria)}
          style={{ display: 'none' }}
          onChange={(event) => {
            const file = event.target.files?.[0];
            // Cleared so that choosing the same file again is a change.
            event.target.value = '';
            if (file) void importFile(file);
          }}
        />
        {error && (
          <Box margin={{ bottom: 'm' }}>
            <Alert type="error" header={error.header} dismissible onDismiss={() => setError(null)}>
              {error.text}
            </Alert>
          </Box>
        )}
        <Modal
          visible={pending !== null}
          onDismiss={() => setPending(null)}
          header={intl.formatMessage(messages.confirmHeader)}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setPending(null)}>
                  {intl.formatMessage(messages.cancel)}
                </Button>
                <Button
                  variant="primary"
                  onClick={() => {
                    if (pending) onImport(pending.routing);
                    setPending(null);
                  }}
                >
                  {intl.formatMessage(messages.replace)}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          {pending && (
            <SpaceBetween size="xs">
              <Box>{intl.formatMessage(messages.confirmBody, { file: pending.file, rules: tableOf(pending.routing).rules.length })}</Box>
              <Box color="text-body-secondary">{intl.formatMessage(messages.confirmNote)}</Box>
            </SpaceBetween>
          )}
        </Modal>
      </>
    );
  },
);
RoutingDmnControls.displayName = 'RoutingDmnControls';

export default RoutingDmnControls;
