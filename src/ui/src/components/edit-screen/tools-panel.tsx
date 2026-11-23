// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen's info panel (the right-hand panel the info icon opens), the same for
// a direct edit, a Production queue item and a Test Studio item.
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type MessageTag, type NoMessageValues } from 'react-intl';

import { DOCS_BASE_URL } from '../../constants/github';
import { PROCESS_CHANGES_LABEL } from '../common/labels';

const messages = defineMessages<{
  processChanges: { readonly b: MessageTag; readonly processChanges: string };
  processChangesDocument: NoMessageValues;
  processChangesQueueItem: { readonly b: MessageTag };
  processChangesTestStudio: NoMessageValues;
  policyRuleValidation: { readonly b: MessageTag };
}>({
  processChanges: {
    id: 'editScreen.toolsPanel.processChanges',
    defaultMessage: '<b>{processChanges}</b> saves, then processes the changes:',
    description:
      'Edit screen info panel, the list of buttons: introduces what Process Changes does, by where the document was opened from. <b> marks the button name; {processChanges} is its label.',
  },
  processChangesDocument: {
    id: 'editScreen.toolsPanel.processChangesDocument',
    defaultMessage:
      'On a document opened from its page, it asks you to confirm and lists the steps of the run that follows. The steps before the one it starts at are not repeated.',
    description: 'Edit screen info panel: what Process Changes does on a document that is not in a review queue.',
  },
  processChangesQueueItem: {
    id: 'editScreen.toolsPanel.processChangesQueueItem',
    defaultMessage:
      'On a queue item, it asks how the review ends and lists the steps of the run that follows. <b>Complete the review</b> keeps your corrections; a section whose pages you changed is extracted again first. <b>Send back for reprocessing</b> discards your corrections and any page regrouping, and the document is processed again from classification (from the beginning in BDA mode).',
    description:
      'Edit screen info panel: what Process Changes does on a review queue item. <b> marks the names of the two options of the dialog it opens. Classification is a processing step; BDA mode is the processing mode that uses Bedrock Data Automation.',
  },
  processChangesTestStudio: {
    id: 'editScreen.toolsPanel.processChangesTestStudio',
    defaultMessage:
      'On a Test Studio document, it also writes the labels as reviewed ground truth. When no draft version of the set is open, it asks first and opens the next version, leaving the current one as it is.',
    description: 'Edit screen info panel: what Process Changes does on a Test Studio document.',
  },
  policyRuleValidation: {
    id: 'editScreen.toolsPanel.policyRuleValidation',
    defaultMessage: '<b>Policy Rule Validation Summary</b>: the policy rules the document passed and failed, with the reason for each.',
    description: 'Edit screen info panel, the Reports menu: what the Policy Rule Validation Summary item shows. <b> marks the item name.',
  },
});

/** Renders a `<b>...</b>` element value: a UI label named inside a sentence. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

const EditScreenToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={<h2>Edit Document</h2>}
    footer={
      <div>
        <h3>
          Learn more <Icon name="external" />
        </h3>
        <ul>
          <li>
            <a href={`${DOCS_BASE_URL}/review-queues/`} target="_blank" rel="noopener noreferrer">
              Review queues
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/human-review/`} target="_blank" rel="noopener noreferrer">
              Human review
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/test-studio/`} target="_blank" rel="noopener noreferrer">
              Test Studio
            </a>
          </li>
        </ul>
      </div>
    }
  >
    <div>
      <p>
        Correct a document&apos;s extracted values, page by page and section by section. The same screen opens from the document page, a
        queue item and a Test Studio document; it shows only the actions that apply.
      </p>
      <h3>Buttons</h3>
      <ul>
        <li>
          <b>Edit | View</b> switches between the Document Editor and the original file. Unsaved edits are kept while the file is shown.
        </li>
        <li>
          <b>Refresh</b> reloads the document; with unsaved edits it asks first.
        </li>
        <li>
          <b>Skip</b> (queue items) gives the item back to its queue with a reason. It is not finished, and saved work is kept.
        </li>
        <li>
          <b>Cancel</b> leaves without finishing, asking first when there are unsaved edits.
        </li>
        <li>
          <b>Save</b> saves the open section without finishing. On a Test Studio document it keeps a draft, which opens no version.
        </li>
        <li>
          <FormattedMessage {...messages.processChanges} values={{ b: bold, processChanges: PROCESS_CHANGES_LABEL }} />
          <ul>
            <li>
              <FormattedMessage {...messages.processChangesDocument} />
            </li>
            <li>
              <FormattedMessage {...messages.processChangesQueueItem} values={{ b: bold }} />
            </li>
            <li>
              <FormattedMessage {...messages.processChangesTestStudio} />
            </li>
          </ul>
        </li>
      </ul>
      <h3>Reports</h3>
      <ul>
        <li>
          <b>History</b>: every saved field edit, and who worked the item.
        </li>
        <li>
          <b>Summary</b>: the document&apos;s summary, once it is finished.
        </li>
        <li>
          <FormattedMessage {...messages.policyRuleValidation} values={{ b: bold }} />
        </li>
        <li>
          <b>Processing Report</b>: how the open section was extracted.
        </li>
      </ul>
      <h3>More Actions</h3>
      <ul>
        <li>
          <b>Use As Evaluation Baseline</b> (Admin and Author): copy a finished document&apos;s results into the evaluation baseline.
        </li>
      </ul>
      <p>A menu item that does not apply yet stays in its menu, disabled; hover it to see why.</p>
      <h3>Document Editor</h3>
      <ul>
        <li>
          Its buttons, right of its title: <b>Show Evaluation</b> (where the document has an evaluation baseline), <b>Section</b>,{' '}
          <b>Show Fields</b> and <b>Edit Page Grouping</b>, which moves pages between sections and changes a section&apos;s class.
        </li>
        <li>Choose a section from the Section list. The page image stays in view while you scroll the fields.</li>
        <li>Click a field to highlight it on the page. Show Fields narrows the list to confidence alerts or evaluation mismatches.</li>
        <li>
          A queue item&apos;s Review Reason lists why it is in review, worst first. Choose a reason, or <b>Next alert</b>, to move to its
          field.
        </li>
        <li>When you cannot edit, a notice at the top says why, with Claim or Take over where you may take the document.</li>
      </ul>
    </div>
  </HelpPanel>
);

export default EditScreenToolsPanel;
