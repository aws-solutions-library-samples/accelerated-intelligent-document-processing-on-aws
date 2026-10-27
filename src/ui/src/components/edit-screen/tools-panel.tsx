// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen's info panel (the right-hand panel the info icon opens), the same for
// a direct edit, a Production queue item and a Test Studio item.
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

import { DOCS_BASE_URL } from '../../constants/github';

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
          <b>Skip</b> (queue items) gives the item back to its queue with a reason. It is not finished, and saved work is kept.
        </li>
        <li>
          <b>Cancel</b> leaves without finishing, asking first when there are unsaved edits.
        </li>
        <li>
          <b>Save</b> saves the open section without finishing.
        </li>
        <li>
          <b>Process Changes</b> saves, then processes the changes: evaluation and summarization re-run, and OCR, classification and
          extraction are not repeated. It finishes a queue item.
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
          <b>Processing Report</b>: how the open section was extracted.
        </li>
      </ul>
      <h3>More Actions</h3>
      <ul>
        <li>
          <b>Edit Page Class</b>: move pages between sections, and change a section&apos;s class.
        </li>
        <li>
          <b>Use As Evaluation Baseline</b> (Admin and Author): copy a finished document&apos;s results into the evaluation baseline.
        </li>
      </ul>
      <p>A menu item that does not apply yet stays in its menu, disabled; hover it to see why.</p>
      <h3>Document Editor</h3>
      <ul>
        <li>Choose a section from the Section list. The page image stays in view while you scroll the fields.</li>
        <li>Click a field to highlight it on the page. Fields to show narrows the list to confidence alerts.</li>
        <li>A queue item&apos;s Review Reason, above the editor, lists why it is in review, worst first.</li>
      </ul>
    </div>
  </HelpPanel>
);

export default EditScreenToolsPanel;
