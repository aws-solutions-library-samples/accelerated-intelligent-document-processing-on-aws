// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type MessageTag, type NoMessageValues } from 'react-intl';

const DOCS_BASE_URL = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';

const messages = defineMessages<{
  title: NoMessageValues;
  learnMore: NoMessageValues;
  docsWebUi: NoMessageValues;
  docsOcrSizing: NoMessageValues;
  intro: NoMessageValues;
  details: NoMessageValues;
  formats: NoMessageValues;
  prefix: { readonly strong: MessageTag };
  queue: NoMessageValues;
  multiple: NoMessageValues;
}>({
  title: {
    id: 'helpPanel.upload.title',
    defaultMessage: 'Upload Documents',
    description: 'Heading of the help panel of the Upload Documents page.',
  },
  learnMore: {
    id: 'helpPanel.upload.learnMore',
    defaultMessage: 'Learn more',
    description: 'Heading of the documentation links at the bottom of the Upload Documents help panel.',
  },
  docsWebUi: {
    id: 'helpPanel.upload.docsWebUi',
    defaultMessage: 'Web UI',
    description: 'Documentation link in the Upload Documents help panel: the page about the web interface.',
  },
  docsOcrSizing: {
    id: 'helpPanel.upload.docsOcrSizing',
    defaultMessage: 'OCR & Image Sizing Guide',
    description: 'Documentation link in the Upload Documents help panel: the guide to text recognition (OCR) and image sizes.',
  },
  intro: {
    id: 'helpPanel.upload.intro',
    defaultMessage: 'Upload documents to be processed by the IDP system.',
    description:
      'First sentence of the Upload Documents help panel. "IDP" is the product abbreviation and may stay as it is in the translation.',
  },
  details: {
    id: 'helpPanel.upload.details',
    defaultMessage: 'Details',
    description: 'Heading above the list of upload details in the Upload Documents help panel.',
  },
  formats: {
    id: 'helpPanel.upload.formats',
    defaultMessage:
      'Supported formats: PDF, PNG, JPEG, TIFF, Excel (XLSX/XLS), CSV, Word (DOCX), and plain text (TXT). PDF and image files are processed with Textract; spreadsheet, Word, and text files are supported through backend conversion. Legacy Word (.doc) is not supported — re-save it as .docx.',
    description:
      'First item of the details list in the Upload Documents help panel: the file types an upload accepts. Format names, file extensions and "Textract" (an AWS service) stay as they are.',
  },
  prefix: {
    id: 'helpPanel.upload.prefix',
    defaultMessage: '<strong>Prefix:</strong> Optionally add a prefix to organize your documents (e.g., "invoices/", "forms/2023/")',
    description:
      'Second item of the details list in the Upload Documents help panel. The <strong> phrase is shown in bold and names the folder prefix field; the quoted examples are folder prefixes.',
  },
  queue: {
    id: 'helpPanel.upload.queue',
    defaultMessage:
      'After upload, documents are automatically added to the processing queue and appear in the Documents list when processing begins',
    description: 'Third item of the details list in the Upload Documents help panel. "Documents" is the name of the document list page.',
  },
  multiple: {
    id: 'helpPanel.upload.multiple',
    defaultMessage: 'Multiple files can be uploaded simultaneously',
    description: 'Fourth item of the details list in the Upload Documents help panel.',
  },
});

const ToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={
      <h2>
        <FormattedMessage {...messages.title} />
      </h2>
    }
    footer={
      <div>
        <h3>
          <FormattedMessage {...messages.learnMore} /> <Icon name="external" />
        </h3>
        <ul>
          <li>
            <a href={`${DOCS_BASE_URL}/web-ui/`} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.docsWebUi} />
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/ocr-image-sizing-guide/`} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.docsOcrSizing} />
            </a>
          </li>
        </ul>
      </div>
    }
  >
    <div>
      <p>
        <FormattedMessage {...messages.intro} />
      </p>
      <h3>
        <FormattedMessage {...messages.details} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.formats} />
        </li>
        <li>
          <FormattedMessage {...messages.prefix} values={{ strong: (chunks) => <strong>{chunks}</strong> }} />
        </li>
        <li>
          <FormattedMessage {...messages.queue} />
        </li>
        <li>
          <FormattedMessage {...messages.multiple} />
        </li>
      </ul>
    </div>
  </HelpPanel>
);

export default ToolsPanel;
