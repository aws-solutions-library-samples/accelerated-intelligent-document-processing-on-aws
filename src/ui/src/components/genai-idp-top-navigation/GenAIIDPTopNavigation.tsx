// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState } from 'react';
import { Box, Button, Modal, SpaceBetween, TopNavigation, Badge } from '@cloudscape-design/components';
import type { TopNavigationProps } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import { signOut } from 'aws-amplify/auth';
import { ConsoleLogger } from 'aws-amplify/utils';

import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import useUserDisplayName from '../../hooks/use-user-display-name';
import useDeploymentContext from '../../hooks/use-deployment-context';
import { useLanguageSwitcherUtility } from '../../i18n/LanguageSwitcher';
import { useInboxBellUtility } from '../inbox/inbox-bell';
import useTenantSwitcher from '../tenant-selection/useTenantSwitcher';
import { clearTenantHint } from '../../utils/tenant-selection';
import { buildBugReportUrl, buildFeatureRequestUrl } from '../../utils/github-feedback';
import { GITHUB_ISSUES_URL } from '../../constants/github';

const logger = new ConsoleLogger('TopNavigation');

const messages = defineMessages<{
  signOutModalCloseAriaLabel: NoMessageValues;
  signOutModalCancel: NoMessageValues;
  signOutModalConfirm: NoMessageValues;
  signOutModalHeader: NoMessageValues;
  signOutModalBody: NoMessageValues;
  feedbackText: NoMessageValues;
  feedbackAriaLabel: NoMessageValues;
  feedbackReportBug: NoMessageValues;
  feedbackRequestFeature: NoMessageValues;
  feedbackViewIssues: NoMessageValues;
  identityDefaultTitle: NoMessageValues;
  roleAdmin: NoMessageValues;
  roleAuthor: NoMessageValues;
  roleReviewer: NoMessageValues;
  roleViewer: NoMessageValues;
  roleAnnotator: NoMessageValues;
  userWithRole: { readonly user: string; readonly role: string };
  userFallback: NoMessageValues;
  overflowMenuTriggerText: NoMessageValues;
  userSignOut: NoMessageValues;
}>({
  signOutModalCloseAriaLabel: {
    id: 'topNavigation.signOutModal.closeAriaLabel',
    defaultMessage: 'Close modal',
    description: 'Screen-reader label of the close (X) button of the sign-out confirmation dialog.',
  },
  signOutModalCancel: {
    id: 'topNavigation.signOutModal.cancel',
    defaultMessage: 'Cancel',
    description: 'Button of the sign-out confirmation dialog that closes it without signing out.',
  },
  signOutModalConfirm: {
    id: 'topNavigation.signOutModal.confirm',
    defaultMessage: 'Sign Out',
    description: 'Primary button of the sign-out confirmation dialog, which signs the user out.',
  },
  signOutModalHeader: {
    id: 'topNavigation.signOutModal.header',
    defaultMessage: 'Sign Out',
    description: 'Title of the sign-out confirmation dialog.',
  },
  signOutModalBody: {
    id: 'topNavigation.signOutModal.body',
    defaultMessage: 'Sign out of the application?',
    description: 'Question asked in the body of the sign-out confirmation dialog.',
  },
  feedbackText: {
    id: 'topNavigation.feedback.text',
    defaultMessage: 'Feedback',
    description: 'Text and tooltip of the feedback menu button in the top navigation, which lists the GitHub feedback links.',
  },
  feedbackAriaLabel: {
    id: 'topNavigation.feedback.ariaLabel',
    defaultMessage: 'Feedback and issue reporting',
    description: 'Screen-reader label of the feedback menu button in the top navigation.',
  },
  feedbackReportBug: {
    id: 'topNavigation.feedback.reportBug',
    defaultMessage: 'Report a bug',
    description: 'Feedback menu item that opens a pre-filled GitHub bug report in a new tab.',
  },
  feedbackRequestFeature: {
    id: 'topNavigation.feedback.requestFeature',
    defaultMessage: 'Request a feature',
    description: 'Feedback menu item that opens a pre-filled GitHub feature request in a new tab.',
  },
  feedbackViewIssues: {
    id: 'topNavigation.feedback.viewIssues',
    defaultMessage: 'View existing issues',
    description: 'Feedback menu item that opens the list of GitHub issues of the project in a new tab.',
  },
  identityDefaultTitle: {
    id: 'topNavigation.identity.defaultTitle',
    defaultMessage: 'AWS IDP Accelerator Console',
    description:
      'Title in the top navigation bar when the deployment sets no console title of its own. "AWS IDP Accelerator" is the product name and may stay as it is in the translation.',
  },
  roleAdmin: {
    id: 'topNavigation.role.admin',
    defaultMessage: 'Admin',
    description: 'Role label of a user in the Admin group, shown next to the name in the user menu of the top navigation.',
  },
  roleAuthor: {
    id: 'topNavigation.role.author',
    defaultMessage: 'Author',
    description: 'Role label of a user in the Author group, shown next to the name in the user menu of the top navigation.',
  },
  roleReviewer: {
    id: 'topNavigation.role.reviewer',
    defaultMessage: 'Reviewer',
    description: 'Role label of a user in the Reviewer group, shown next to the name in the user menu of the top navigation.',
  },
  roleViewer: {
    id: 'topNavigation.role.viewer',
    defaultMessage: 'Viewer',
    description: 'Role label of a user in the Viewer group, shown next to the name in the user menu of the top navigation.',
  },
  roleAnnotator: {
    id: 'topNavigation.role.annotator',
    defaultMessage: 'Annotator',
    description: 'Role label of a user in the Annotator group, shown next to the name in the user menu of the top navigation.',
  },
  userWithRole: {
    id: 'topNavigation.user.withRole',
    defaultMessage: '{user} ({role})',
    description:
      'Text of the user menu button in the top navigation. {user} is the display name or username, {role} the role label (Admin, Author, Reviewer, Viewer or Annotator).',
  },
  userFallback: {
    id: 'topNavigation.user.fallback',
    defaultMessage: 'user',
    description:
      'Stands in for the name in the user menu of the top navigation when the signed-in user has neither a display name nor a username.',
  },
  overflowMenuTriggerText: {
    id: 'topNavigation.overflowMenuTriggerText',
    defaultMessage: 'More',
    description: 'Text of the button that opens the collapsed utilities when the top navigation is too narrow to show them all.',
  },
  userSignOut: {
    id: 'topNavigation.user.signOut',
    defaultMessage: 'Sign out',
    description: 'The item of the user menu in the top navigation that opens the sign-out confirmation dialog.',
  },
});

interface SignOutModalProps {
  visible: boolean;
  setVisible: (visible: boolean) => void;
}

const SignOutModal = ({ visible, setVisible }: SignOutModalProps): React.JSX.Element => {
  const intl = useIntl();
  async function handleSignOut() {
    try {
      // Set flag to prevent auto-login from immediately signing back in via SSO
      sessionStorage.setItem('idp_signed_out', 'true');
      clearTenantHint();

      // Amplify's signOut() handles both federated and non-federated flows:
      // - Clears local tokens from localStorage
      // - When OAuth is configured, redirects through Cognito's /logout endpoint
      //   using the redirectSignOut URL from aws-exports.js
      await signOut();
      logger.debug('signed out');
      window.location.reload();
    } catch (error) {
      logger.error('error signing out: ', error);
    }
  }
  return (
    <Modal
      onDismiss={() => setVisible(false)}
      visible={visible}
      closeAriaLabel={intl.formatMessage(messages.signOutModalCloseAriaLabel)}
      size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => setVisible(false)}>
              <FormattedMessage {...messages.signOutModalCancel} />
            </Button>
            <Button variant="primary" onClick={() => handleSignOut()}>
              <FormattedMessage {...messages.signOutModalConfirm} />
            </Button>
          </SpaceBetween>
        </Box>
      }
      header={intl.formatMessage(messages.signOutModalHeader)}
    >
      <FormattedMessage {...messages.signOutModalBody} />
    </Modal>
  );
};

const GenAIIDPTopNavigation = (): React.JSX.Element => {
  const intl = useIntl();
  const { user } = useAppContext();
  const { settings } = useSettingsContext();
  const {
    isAdmin,
    isAuthor,
    isReviewer,
    isViewer,
    isAnnotator,
    isAnnotatorOnly,
    eligibleTenants,
    activeTenant,
    subject,
    loading: roleLoading,
  } = useUserRole();
  // Present only for a user the identity provider granted several tenants.
  const tenantSwitcher = useTenantSwitcher({ eligibleTenants, activeTenant, subject });
  const { displayName } = useUserDisplayName();
  const deploymentContext = useDeploymentContext();
  const inboxUtility = useInboxBellUtility();
  // null while only one locale is enabled, so nothing is added to the utilities.
  const languageSwitcherUtility = useLanguageSwitcherUtility();
  const userId = displayName || user?.username || intl.formatMessage(messages.userFallback);
  const [isSignOutModalVisible, setIsSignOutModalVisiblesetVisible] = useState(false);

  // Pre-filled GitHub feedback/issue links. The feedback menu is available to
  // every role except an Annotator-only user; the URLs auto-populate the issue-form environment fields from
  // the current deployment (version/region/stack/pattern). Items use `href` +
  // `external` so they render as real links (middle-click / open-in-new-tab
  // work) and open in a new tab via onItemFollow.
  const bugReportUrl = buildBugReportUrl(deploymentContext);
  const featureRequestUrl = buildFeatureRequestUrl(deploymentContext);
  const feedbackUtility: TopNavigationProps.Utility = {
    type: 'menu-dropdown',
    iconName: 'contact',
    text: intl.formatMessage(messages.feedbackText),
    title: intl.formatMessage(messages.feedbackText),
    ariaLabel: intl.formatMessage(messages.feedbackAriaLabel),
    items: [
      { id: 'report-bug', text: intl.formatMessage(messages.feedbackReportBug), href: bugReportUrl, external: true, iconName: 'bug' },
      {
        id: 'request-feature',
        text: intl.formatMessage(messages.feedbackRequestFeature),
        href: featureRequestUrl,
        external: true,
        iconName: 'suggestions',
      },
      {
        id: 'view-issues',
        text: intl.formatMessage(messages.feedbackViewIssues),
        href: GITHUB_ISSUES_URL,
        external: true,
        iconName: 'external',
      },
    ],
  };

  // Banner title is configurable per-deployment so vertical-product packs
  // (Claims, Loans, etc.) can swap the default "AWS IDP Accelerator Console"
  // text without rebuilding the UI. Set ConsoleTitle in the host's SSM
  // Settings parameter (driven by the ConsoleTitle CFN parameter).
  const consoleTitle =
    ((settings as Record<string, unknown> | undefined)?.ConsoleTitle as string | undefined) ||
    intl.formatMessage(messages.identityDefaultTitle);

  // Determine role display
  const getRoleDisplay = (): string => {
    if (roleLoading) return '';
    if (isAdmin) return intl.formatMessage(messages.roleAdmin);
    if (isAuthor) return intl.formatMessage(messages.roleAuthor);
    if (isReviewer) return intl.formatMessage(messages.roleReviewer);
    if (isViewer) return intl.formatMessage(messages.roleViewer);
    if (isAnnotator) return intl.formatMessage(messages.roleAnnotator);
    return '';
  };

  const roleDisplay = getRoleDisplay();
  const userDisplayText = roleDisplay ? intl.formatMessage(messages.userWithRole, { user: userId, role: roleDisplay }) : userId;

  return (
    <>
      <div id="top-navigation" style={{ position: 'sticky', top: 0, zIndex: 1002 }}>
        <TopNavigation
          identity={{ href: '#', title: consoleTitle }}
          i18nStrings={{ overflowMenuTriggerText: intl.formatMessage(messages.overflowMenuTriggerText) }}
          utilities={[
            // The notifications bell belongs to the Inbox, which every user has.
            inboxUtility,
            ...(tenantSwitcher.utility ? [tenantSwitcher.utility] : []),
            // An Annotator-only user's shell is Home and the notifications bell.
            ...(isAnnotatorOnly ? [] : [feedbackUtility]),
            // The language menu, present only once a second locale is enabled.
            ...(languageSwitcherUtility ? [languageSwitcherUtility] : []),
            {
              type: 'menu-dropdown',
              text: userDisplayText,
              ...({
                description: roleDisplay ? (
                  <SpaceBetween direction="horizontal" size="xs">
                    <span>{userId}</span>
                    <Badge color={isAdmin ? 'blue' : isAuthor ? 'green' : 'grey'}>{roleDisplay}</Badge>
                  </SpaceBetween>
                ) : (
                  userId
                ),
              } as Record<string, unknown>),
              iconName: 'user-profile',
              items: [
                {
                  id: 'signout',
                  text: intl.formatMessage(messages.userSignOut),
                  ...({ type: 'button' } as Record<string, unknown>),
                  ...({
                    text: (
                      <Button variant="primary" onClick={() => setIsSignOutModalVisiblesetVisible(true)}>
                        <FormattedMessage {...messages.userSignOut} />
                      </Button>
                    ),
                  } as Record<string, unknown>),
                },
              ],
            },
          ]}
        />
      </div>
      <SignOutModal visible={isSignOutModalVisible} setVisible={setIsSignOutModalVisiblesetVisible} />
      {tenantSwitcher.modal}
    </>
  );
};

export default GenAIIDPTopNavigation;
