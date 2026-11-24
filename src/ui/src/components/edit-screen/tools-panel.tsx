// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen's info panel (the right-hand panel the info icon opens), the same for
// a direct edit, a Production queue item and a Test Studio item.
import React from 'react';
import { HelpPanel } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

import LearnMoreLinks from '../common/LearnMoreLinks';
import { sharedLabelMessages } from '../common/labels';

type Bold = { readonly b: MessageTag };

/** Renders a `<b>...</b>` element value: a control's name inside a sentence. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

const messages = defineMessages<{
  title: NoMessageValues;
  intro: NoMessageValues;
  headingButtons: NoMessageValues;
  buttonEditView: Bold;
  buttonRefresh: Bold;
  buttonSkip: Bold;
  buttonCancel: Bold;
  buttonSave: Bold;
  buttonProcessChanges: Bold & { readonly processChanges: string };
  headingReports: NoMessageValues;
  reportHistory: Bold;
  reportSummary: Bold;
  reportProcessing: Bold;
  headingMoreActions: NoMessageValues;
  moreActionsBaseline: Bold;
  menuNote: NoMessageValues;
  headingDocumentEditor: NoMessageValues;
  editorButtons: Bold;
  editorSection: NoMessageValues;
  editorField: NoMessageValues;
  editorReviewReason: Bold & { readonly nextAlert: string };
  editorReadOnly: NoMessageValues;
  linkReviewQueues: NoMessageValues;
  linkHumanReview: NoMessageValues;
  linkTestStudio: NoMessageValues;
}>({
  title: {
    id: 'editScreen.toolsPanel.title',
    defaultMessage: 'Edit Document',
    description: "Title of the edit page's help panel.",
  },
  intro: {
    id: 'editScreen.toolsPanel.intro',
    defaultMessage:
      "Correct a document's extracted values, page by page and section by section. The same screen opens from the document page, a queue item and a Test Studio document; it shows only the actions that apply.",
    description:
      "Opening paragraph of the edit page's help panel. The edit page opens from the document page, from a queue item (a review queue item) and from a Test Studio document (a test-set item).",
  },
  headingButtons: {
    id: 'editScreen.toolsPanel.heading.buttons',
    defaultMessage: 'Buttons',
    description: "Sub-heading in the edit page's help panel, above the notes on the controls of the page's action bar.",
  },
  buttonEditView: {
    id: 'editScreen.toolsPanel.button.editView',
    defaultMessage:
      '<b>Edit | View</b> switches between the Document Editor and the original file. Unsaved edits are kept while the file is shown.',
    description:
      "Help panel note on the edit page's action bar. <b>Edit | View</b> names the switch first in the bar by its two options, in bold. Document Editor is the title of the edit page's section editor.",
  },
  buttonRefresh: {
    id: 'editScreen.toolsPanel.button.refresh',
    defaultMessage: '<b>Refresh</b> reloads the document; with unsaved edits it asks first.',
    description: "Help panel note on the edit page's action bar. <b>Refresh</b> is the label of a button on the bar, in bold.",
  },
  buttonSkip: {
    id: 'editScreen.toolsPanel.button.skip',
    defaultMessage: '<b>Skip</b> (queue items) gives the item back to its queue with a reason. It is not finished, and saved work is kept.',
    description: "Help panel note on the edit page's action bar. <b>Skip</b> is the label of a button on the bar, in bold.",
  },
  buttonCancel: {
    id: 'editScreen.toolsPanel.button.cancel',
    defaultMessage: '<b>Cancel</b> leaves without finishing, asking first when there are unsaved edits.',
    description: "Help panel note on the edit page's action bar. <b>Cancel</b> is the label of a button on the bar, in bold.",
  },
  buttonSave: {
    id: 'editScreen.toolsPanel.button.save',
    defaultMessage:
      '<b>Save</b> saves the open section without finishing. On a Test Studio document it keeps a draft, which opens no version.',
    description:
      "Help panel note on the edit page's action bar. <b>Save</b> is the label of a button on the bar, in bold. A Test Studio document is a test-set item; a version is a version of its test set.",
  },
  buttonProcessChanges: {
    id: 'editScreen.toolsPanel.button.processChanges',
    defaultMessage:
      '<b>{processChanges}</b> saves, then processes the changes: evaluation and summarization re-run, and OCR, classification and extraction are not repeated. It finishes a queue item. On a Test Studio document it also writes the labels as reviewed ground truth; when no draft version of the set is open, it asks first and opens the next version, leaving the current one as it is.',
    description:
      "Help panel note on the edit page's action bar. {processChanges} is the label of the bar's Process Changes button, in bold. A queue item is a review queue item; a Test Studio document is a test-set item, and the set is its test set.",
  },
  headingReports: {
    id: 'editScreen.toolsPanel.heading.reports',
    defaultMessage: 'Reports',
    description: "Sub-heading in the edit page's help panel, above the notes on the Reports menu of the action bar.",
  },
  reportHistory: {
    id: 'editScreen.toolsPanel.report.history',
    defaultMessage: '<b>History</b>: every saved field edit, and who worked the item.',
    description: "Help panel note on the Reports menu of the edit page's action bar. <b>History</b> is the label of a menu item, in bold.",
  },
  reportSummary: {
    id: 'editScreen.toolsPanel.report.summary',
    defaultMessage: "<b>Summary</b>: the document's summary, once it is finished.",
    description: "Help panel note on the Reports menu of the edit page's action bar. <b>Summary</b> is the label of a menu item, in bold.",
  },
  reportProcessing: {
    id: 'editScreen.toolsPanel.report.processing',
    defaultMessage: '<b>Processing Report</b>: how the open section was extracted.',
    description:
      "Help panel note on the Reports menu of the edit page's action bar. <b>Processing Report</b> is the label of a menu item, in bold.",
  },
  headingMoreActions: {
    id: 'editScreen.toolsPanel.heading.moreActions',
    defaultMessage: 'More Actions',
    description: "Sub-heading in the edit page's help panel, above the notes on the More Actions menu of the action bar.",
  },
  moreActionsBaseline: {
    id: 'editScreen.toolsPanel.moreActions.baseline',
    defaultMessage:
      "<b>Use As Evaluation Baseline</b> (Admin and Author): copy a finished document's results into the evaluation baseline.",
    description:
      "Help panel note on the More Actions menu of the edit page's action bar. <b>Use As Evaluation Baseline</b> is the label of a menu item, in bold; Admin and Author are user roles.",
  },
  menuNote: {
    id: 'editScreen.toolsPanel.menuNote',
    defaultMessage: 'A menu item that does not apply yet stays in its menu, disabled; hover it to see why.',
    description:
      "Paragraph of the edit page's help panel, below the notes on the Reports and More Actions menus: why a menu item can be disabled.",
  },
  headingDocumentEditor: {
    id: 'editScreen.toolsPanel.heading.documentEditor',
    defaultMessage: 'Document Editor',
    description:
      "Sub-heading in the edit page's help panel, above the notes on the section editor. Document Editor is the section editor's title.",
  },
  editorButtons: {
    id: 'editScreen.toolsPanel.editor.buttons',
    defaultMessage:
      "Its buttons, right of its title: <b>Show Evaluation</b> (where the document has an evaluation baseline), <b>Section</b>, <b>Show Fields</b> and <b>Edit Page Grouping</b>, which moves pages between sections and changes a section's class.",
    description:
      'Help panel note on the Document Editor, the edit page\'s section editor ("its" is the Document Editor\'s). Each bold text names a control in its header: the Show Evaluation toggle, the Section menu, the menu that chooses which fields show, and the Edit Page Grouping button.',
  },
  editorSection: {
    id: 'editScreen.toolsPanel.editor.section',
    defaultMessage: 'Choose a section from the Section list. The page image stays in view while you scroll the fields.',
    description:
      "Help panel note on the Document Editor, the edit page's section editor. The Section list is the menu in its header that opens a section.",
  },
  editorField: {
    id: 'editScreen.toolsPanel.editor.field',
    defaultMessage:
      'Click a field to highlight it on the page. Show Fields narrows the list to confidence alerts or evaluation mismatches.',
    description:
      "Help panel note on the Document Editor, the edit page's section editor. Show Fields names the menu in its header that chooses which fields show.",
  },
  editorReviewReason: {
    id: 'editScreen.toolsPanel.editor.reviewReason',
    defaultMessage:
      "A queue item's Review Reason lists why it is in review, worst first. Choose a reason, or <b>{nextAlert}</b>, to move to its field.",
    description:
      "Help panel note on the Review Reason of a review queue item, the panel above the editor that lists why the item is in review. {nextAlert}, in bold, is the label of that panel's Next alert button, which moves to the next field with a confidence alert.",
  },
  editorReadOnly: {
    id: 'editScreen.toolsPanel.editor.readOnly',
    defaultMessage: 'When you cannot edit, a notice at the top says why, with Claim or Take over where you may take the document.',
    description:
      'Help panel note on the notice at the top of the edit page when its fields are read-only. Claim and Take over are the labels of buttons that come with that notice.',
  },
  linkReviewQueues: {
    id: 'editScreen.toolsPanel.link.reviewQueues',
    defaultMessage: 'Review queues',
    description: 'Label of a "Learn more" link in the edit page\'s help panel, to the Review queues documentation page.',
  },
  linkHumanReview: {
    id: 'editScreen.toolsPanel.link.humanReview',
    defaultMessage: 'Human review',
    description: 'Label of a "Learn more" link in the edit page\'s help panel, to the Human review documentation page.',
  },
  linkTestStudio: {
    id: 'editScreen.toolsPanel.link.testStudio',
    defaultMessage: 'Test Studio',
    description:
      'Label of a "Learn more" link in the edit page\'s help panel, to the Test Studio documentation page. Test Studio is a product area name.',
  },
});

const EditScreenToolsPanel = (): React.JSX.Element => {
  const intl = useIntl();
  return (
    <HelpPanel
      header={
        <h2>
          <FormattedMessage {...messages.title} />
        </h2>
      }
      footer={
        <LearnMoreLinks
          links={[
            { text: <FormattedMessage {...messages.linkReviewQueues} />, docsPath: 'review-queues/' },
            { text: <FormattedMessage {...messages.linkHumanReview} />, docsPath: 'human-review/' },
            { text: <FormattedMessage {...messages.linkTestStudio} />, docsPath: 'test-studio/' },
          ]}
        />
      }
    >
      <div>
        <p>
          <FormattedMessage {...messages.intro} />
        </p>
        <h3>
          <FormattedMessage {...messages.headingButtons} />
        </h3>
        <ul>
          <li>
            <FormattedMessage {...messages.buttonEditView} values={{ b: bold }} />
          </li>
          <li>
            <FormattedMessage {...messages.buttonRefresh} values={{ b: bold }} />
          </li>
          <li>
            <FormattedMessage {...messages.buttonSkip} values={{ b: bold }} />
          </li>
          <li>
            <FormattedMessage {...messages.buttonCancel} values={{ b: bold }} />
          </li>
          <li>
            <FormattedMessage {...messages.buttonSave} values={{ b: bold }} />
          </li>
          <li>
            <FormattedMessage
              {...messages.buttonProcessChanges}
              values={{ b: bold, processChanges: intl.formatMessage(sharedLabelMessages.processChanges) }}
            />
          </li>
        </ul>
        <h3>
          <FormattedMessage {...messages.headingReports} />
        </h3>
        <ul>
          <li>
            <FormattedMessage {...messages.reportHistory} values={{ b: bold }} />
          </li>
          <li>
            <FormattedMessage {...messages.reportSummary} values={{ b: bold }} />
          </li>
          <li>
            <FormattedMessage {...messages.reportProcessing} values={{ b: bold }} />
          </li>
        </ul>
        <h3>
          <FormattedMessage {...messages.headingMoreActions} />
        </h3>
        <ul>
          <li>
            <FormattedMessage {...messages.moreActionsBaseline} values={{ b: bold }} />
          </li>
        </ul>
        <p>
          <FormattedMessage {...messages.menuNote} />
        </p>
        <h3>
          <FormattedMessage {...messages.headingDocumentEditor} />
        </h3>
        <ul>
          <li>
            <FormattedMessage {...messages.editorButtons} values={{ b: bold }} />
          </li>
          <li>
            <FormattedMessage {...messages.editorSection} />
          </li>
          <li>
            <FormattedMessage {...messages.editorField} />
          </li>
          <li>
            <FormattedMessage
              {...messages.editorReviewReason}
              values={{ b: bold, nextAlert: intl.formatMessage(sharedLabelMessages.nextAlert) }}
            />
          </li>
          <li>
            <FormattedMessage {...messages.editorReadOnly} />
          </li>
        </ul>
      </div>
    </HelpPanel>
  );
};

export default EditScreenToolsPanel;
