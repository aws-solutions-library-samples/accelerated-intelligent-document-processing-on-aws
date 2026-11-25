// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useMemo } from 'react';
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type MessageTag, type NoMessageValues } from 'react-intl';
import { useLocation } from 'react-router-dom';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { Badge, Box, Button, Link, Popover, SideNavigation, SpaceBetween } from '@cloudscape-design/components';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import useInstalledFeatures from '../../hooks/use-installed-features';
import useCatalogFeatures from '../../hooks/use-catalog-features';
import { BROWSE_CATALOG_HREF, buildFeaturesNavSection, COMING_SOON_HREF } from './feature-platform-nav-items';
import useLatestVersion from '../../hooks/use-latest-version';
import useInbox from '../../hooks/use-inbox';
import { buildInboxNavLink } from '../inbox/inbox-nav-link';
import { TourHotspot } from '../../contexts/tourHotspots';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import { buildReviewStudioNavSection, buildHomeNavSection } from './review-queues-nav-items';
import useQuickStartAvailable from '../../hooks/use-quick-start-available';
import './navigation.css';

import {
  DOCUMENTS_PATH,
  DOCUMENTS_KB_QUERY_PATH,
  TEST_STUDIO_PATH,
  DEFAULT_PATH,
  UPLOAD_DOCUMENT_PATH,
  CONFIGURATION_PATH,
  PRICING_PATH,
  MODEL_CONFIG_LIMITS_PATH,
  DISCOVERY_PATH,
  USER_MANAGEMENT_PATH,
  MCP_CONFIGURATION_PATH,
  CONFIG_PREFIX_MAPPINGS_PATH,
  AGENT_CHAT_PATH,
  CAPACITY_PLANNING_PATH,
  CUSTOM_MODELS_PATH,
  FEATURES_PATH_PREFIX,
  INBOX_PATH,
  MY_QUEUES_PATH,
  MY_TASKS_PATH,
  MY_WORK_PATH,
  JOBS_PATH,
  REVIEW_QUEUES_PATH,
} from '../../routes/constants';

const messages = defineMessages<{
  documentsSection: NoMessageValues;
  documentList: NoMessageValues;
  uploadDocuments: NoMessageValues;
  assistantsSection: NoMessageValues;
  knowledgeBase: NoMessageValues;
  agentChat: NoMessageValues;
  testStudioSection: NoMessageValues;
  testSets: NoMessageValues;
  testExecutions: NoMessageValues;
  configurationSection: NoMessageValues;
  viewEditConfiguration: NoMessageValues;
  viewConfiguration: NoMessageValues;
  discovery: NoMessageValues;
  customModels: NoMessageValues;
  customModelsRegionNote: NoMessageValues;
  customModelsRegionBadge: NoMessageValues;
  capacityPlanning: NoMessageValues;
  userManagement: NoMessageValues;
  prefixMappings: NoMessageValues;
  mcpConfiguration: NoMessageValues;
  viewEditPricing: NoMessageValues;
  viewPricing: NoMessageValues;
  viewEditModelLimits: NoMessageValues;
  viewModelLimits: NoMessageValues;
  resourcesSection: NoMessageValues;
  blog: NoMessageValues;
  documentation: NoMessageValues;
  sourceCode: NoMessageValues;
  reportIssue: NoMessageValues;
  deploymentInfoSection: NoMessageValues;
  stackName: { readonly stackName: string };
  version: { readonly version: string };
  build: { readonly buildDateTime: string };
  pattern: { readonly pattern: string };
  region: { readonly region: string };
  updateAvailable: NoMessageValues;
  updateBadge: NoMessageValues;
  latestVersion: { readonly version: string; readonly strong: MessageTag };
  currentVersion: { readonly version: string; readonly strong: MessageTag };
  updateStackLink: NoMessageValues;
  updateStackHelp: NoMessageValues;
  contactAdministrator: NoMessageValues;
  quickStart: NoMessageValues;
}>({
  documentsSection: {
    id: 'navigation.documents.section',
    defaultMessage: 'Documents',
    description: 'Side navigation section that holds the document list and the upload page.',
  },
  documentList: {
    id: 'navigation.documents.list',
    defaultMessage: 'Document List',
    description: 'Side navigation link, in the Documents section, to the list of processed documents.',
  },
  uploadDocuments: {
    id: 'navigation.documents.upload',
    defaultMessage: 'Upload Documents',
    description: 'Side navigation link, in the Documents section, to the page that uploads documents for processing.',
  },
  assistantsSection: {
    id: 'navigation.assistants.section',
    defaultMessage: 'Assistants',
    description: 'Side navigation section that holds the Knowledge Base and Agent Chat assistants. Admin users only.',
  },
  knowledgeBase: {
    id: 'navigation.assistants.knowledgeBase',
    defaultMessage: 'Knowledge Base',
    description: 'Side navigation link, in the Assistants section, to the page that queries the knowledge base built from the documents.',
  },
  agentChat: {
    id: 'navigation.assistants.agentChat',
    defaultMessage: 'Agent Chat',
    description: 'Side navigation link, in the Assistants section, to the chat with the analytics agent.',
  },
  testStudioSection: {
    id: 'navigation.testStudio.section',
    defaultMessage: 'Test Studio',
    description:
      'Side navigation section that holds the test sets and the test executions. "Test Studio" is the name of that part of the application.',
  },
  testSets: {
    id: 'navigation.testStudio.sets',
    defaultMessage: 'Test Sets',
    description: 'Side navigation link, in the Test Studio section, to the Test Sets tab.',
  },
  testExecutions: {
    id: 'navigation.testStudio.executions',
    defaultMessage: 'Test Executions',
    description: 'Side navigation link, in the Test Studio section, to the Test Executions tab, which lists the test runs.',
  },
  configurationSection: {
    id: 'navigation.configuration.section',
    defaultMessage: 'Configuration',
    description: 'Side navigation section that holds the configuration pages (processing configuration, discovery, pricing, users).',
  },
  viewEditConfiguration: {
    id: 'navigation.configuration.viewEdit',
    defaultMessage: 'View/Edit Configuration',
    description:
      'Side navigation link, in the Configuration section, to the processing configuration page, for users who may edit it (Admin, Author).',
  },
  viewConfiguration: {
    id: 'navigation.configuration.view',
    defaultMessage: 'View Configuration',
    description:
      'Side navigation link, in the Configuration section, to the processing configuration page, for users who may only read it (Viewer).',
  },
  discovery: {
    id: 'navigation.configuration.discovery',
    defaultMessage: 'Discovery',
    description:
      'Side navigation link, in the Configuration section, to the page that discovers document classes and attributes from sample documents.',
  },
  customModels: {
    id: 'navigation.configuration.customModels',
    defaultMessage: 'Custom Models',
    description: 'Side navigation link, in the Configuration section, to the page that fine-tunes custom models.',
  },
  customModelsRegionNote: {
    id: 'navigation.configuration.customModelsRegionNote',
    defaultMessage:
      'Custom Models requires Amazon Nova fine-tuning, which is currently available in us-east-1 only. Deploy your stack in us-east-1 to use this feature.',
    description:
      'Popover text behind the badge of the Custom Models link when the deployment is not in the us-east-1 AWS Region, where that link is greyed out. "us-east-1" is a Region code and stays as it is.',
  },
  customModelsRegionBadge: {
    id: 'navigation.configuration.customModelsRegionBadge',
    defaultMessage: 'us-east-1 only',
    description:
      'Badge next to the Custom Models link when the deployment is not in the us-east-1 AWS Region, where that link is greyed out. "us-east-1" is a Region code and stays as it is.',
  },
  capacityPlanning: {
    id: 'navigation.configuration.capacityPlanning',
    defaultMessage: 'Capacity Planning',
    description: 'Side navigation link, in the Configuration section, to the page that plans model throughput capacity.',
  },
  userManagement: {
    id: 'navigation.configuration.userManagement',
    defaultMessage: 'User Management',
    description:
      'Side navigation link, in the Configuration section, to the page that manages the users of the application. Admin users only.',
  },
  prefixMappings: {
    id: 'navigation.configuration.prefixMappings',
    defaultMessage: 'Prefix Mappings',
    description:
      'Side navigation link, in the Configuration section, to the page that maps upload prefixes to configuration profiles. Admin users only.',
  },
  mcpConfiguration: {
    id: 'navigation.configuration.mcpConfiguration',
    defaultMessage: 'MCP Configuration',
    description: 'Side navigation link, in the Configuration section, to the page that manages MCP server credentials. Admin users only.',
  },
  viewEditPricing: {
    id: 'navigation.configuration.viewEditPricing',
    defaultMessage: 'View / Edit Pricing',
    description: 'Side navigation link, in the Configuration section, to the model pricing page, for Admin users, who may edit it.',
  },
  viewPricing: {
    id: 'navigation.configuration.viewPricing',
    defaultMessage: 'View Pricing',
    description:
      'Side navigation link, in the Configuration section, to the model pricing page, for users who may only read it (Author, Viewer).',
  },
  viewEditModelLimits: {
    id: 'navigation.configuration.viewEditModelLimits',
    defaultMessage: 'View / Edit Model Limits',
    description: 'Side navigation link, in the Configuration section, to the model limits page, for Admin users, who may edit it.',
  },
  viewModelLimits: {
    id: 'navigation.configuration.viewModelLimits',
    defaultMessage: 'View Model Limits',
    description:
      'Side navigation link, in the Configuration section, to the model limits page, for users who may only read it (Author, Viewer).',
  },
  resourcesSection: {
    id: 'navigation.resources.section',
    defaultMessage: 'Resources',
    description: 'Side navigation section of external links: the blog, the documentation, the source code and the issue tracker.',
  },
  blog: {
    id: 'navigation.resources.blog',
    defaultMessage: 'Blog',
    description: 'Side navigation link, in the Resources section, to the blog post about the solution (opens in a new tab).',
  },
  documentation: {
    id: 'navigation.resources.documentation',
    defaultMessage: 'Documentation',
    description: 'Side navigation link, in the Resources section, to the published documentation of the solution (opens in a new tab).',
  },
  sourceCode: {
    id: 'navigation.resources.sourceCode',
    defaultMessage: 'Source Code',
    description: 'Side navigation link, in the Resources section, to the source repository of the solution on GitHub (opens in a new tab).',
  },
  reportIssue: {
    id: 'navigation.resources.reportIssue',
    defaultMessage: 'Report an issue',
    description: 'Side navigation link, in the Resources section, to the issue tracker of the solution on GitHub (opens in a new tab).',
  },
  deploymentInfoSection: {
    id: 'navigation.deploymentInfo.section',
    defaultMessage: 'Deployment Info',
    description:
      'Last section of the side navigation: facts about this deployment (stack name, version, build, pattern, Region). Its entries are not links.',
  },
  stackName: {
    id: 'navigation.deploymentInfo.stackName',
    defaultMessage: 'Stack Name: {stackName}',
    description: 'Entry of the Deployment Info section. {stackName} is the name of the CloudFormation stack of this deployment.',
  },
  version: {
    id: 'navigation.deploymentInfo.version',
    defaultMessage: 'Version: {version}',
    description: 'Entry of the Deployment Info section. {version} is the version number of the deployed solution, such as 0.4.2.',
  },
  build: {
    id: 'navigation.deploymentInfo.build',
    defaultMessage: 'Build: {buildDateTime}',
    description:
      'Entry of the Deployment Info section. {buildDateTime} is the build timestamp of the deployed solution, as the build wrote it.',
  },
  pattern: {
    id: 'navigation.deploymentInfo.pattern',
    defaultMessage: 'Pattern: {pattern}',
    description:
      'Entry of the Deployment Info section. {pattern} is the name of the deployed processing pattern, such as Pattern-2 or Unified.',
  },
  region: {
    id: 'navigation.deploymentInfo.region',
    defaultMessage: 'Region: {region}',
    description: 'Entry of the Deployment Info section. {region} is the code of the AWS Region of this deployment, such as us-east-1.',
  },
  updateAvailable: {
    id: 'navigation.deploymentInfo.updateAvailable',
    defaultMessage: 'Update available',
    description:
      'Header of the popover behind the Update badge of the Version entry, shown when a newer version of the solution is published.',
  },
  updateBadge: {
    id: 'navigation.deploymentInfo.updateBadge',
    defaultMessage: 'Update',
    description:
      'Badge next to the Version entry of the Deployment Info section when a newer version of the solution is published. Opens the popover.',
  },
  latestVersion: {
    id: 'navigation.deploymentInfo.latestVersion',
    defaultMessage: 'Latest published version: <strong>v{version}</strong>',
    description:
      'First line of the Update available popover. {version} is the newest published version number, such as 0.4.3; <strong> puts it in bold.',
  },
  currentVersion: {
    id: 'navigation.deploymentInfo.currentVersion',
    defaultMessage: 'Currently deployed: <strong>v{version}</strong>',
    description:
      'Second line of the Update available popover. {version} is the version number deployed here, such as 0.4.2; <strong> puts it in bold.',
  },
  updateStackLink: {
    id: 'navigation.deploymentInfo.updateStackLink',
    defaultMessage: 'Update stack in CloudFormation →',
    description:
      'Link in the Update available popover, for Admin users, that opens the AWS CloudFormation console on the stack update page (new tab).',
  },
  updateStackHelp: {
    id: 'navigation.deploymentInfo.updateStackHelp',
    defaultMessage: 'Opens the AWS CloudFormation console with the new template URL pre-filled. Review parameters before applying.',
    description: 'Small print under the CloudFormation link of the Update available popover, for Admin users.',
  },
  contactAdministrator: {
    id: 'navigation.deploymentInfo.contactAdministrator',
    defaultMessage: 'Contact your administrator to update this deployment to the latest version.',
    description: 'Small print of the Update available popover for users who are not Admins and cannot update the stack themselves.',
  },
  quickStart: {
    id: 'navigation.quickStart.button',
    defaultMessage: 'Quick Start',
    description: 'Button above the side navigation that opens the Quick Start guide of the application.',
  },
});

const MCP_CONFIGURATION_HREF = `#${MCP_CONFIGURATION_PATH}`;
const CAPACITY_PLANNING_HREF = `#${CAPACITY_PLANNING_PATH}`;
const CUSTOM_MODELS_HREF = `#${CUSTOM_MODELS_PATH}`;
// The first link of the Configuration, Test Studio and Resources sections. A section has
// no href of its own and its text follows the UI's language, so the code below finds a
// section by the link that leads it (see isSectionLedBy).
const CONFIGURATION_HREF = `#${CONFIGURATION_PATH}`;
const TEST_SETS_HREF = `#${TEST_STUDIO_PATH}?tab=sets`;
const BLOG_URL = 'https://www.amazon.com/genai-idp-accelerator';

/** Whether `item` is the section whose first link is `href`. */
const isSectionLedBy = (item: unknown, href: string): boolean => {
  const section = item as { type?: string; items?: ReadonlyArray<{ href?: string }> };
  return section.type === 'section' && section.items?.[0]?.href === href;
};

const strong = (chunks: React.ReactNode[]): React.JSX.Element => <strong>{chunks}</strong>;

const NAV_HOTSPOTS: Record<string, string> = {
  [`#${DOCUMENTS_PATH}`]: 'nav-documents',
  [`#${UPLOAD_DOCUMENT_PATH}`]: 'nav-upload',
  [`#${DOCUMENTS_KB_QUERY_PATH}`]: 'nav-document-kb',
  [`#${AGENT_CHAT_PATH}`]: 'nav-agent-chat',
  [`#${CONFIGURATION_PATH}`]: 'nav-configuration',
  [`#${DISCOVERY_PATH}`]: 'nav-discovery',
  [`#${CUSTOM_MODELS_PATH}`]: 'nav-custom-models',
  [`#${CAPACITY_PLANNING_PATH}`]: 'nav-capacity-planning',
  [`#${USER_MANAGEMENT_PATH}`]: 'nav-user-management',
  [`#${CONFIG_PREFIX_MAPPINGS_PATH}`]: 'nav-config-prefix-mappings',
  [`#${PRICING_PATH}`]: 'nav-pricing',
  [`#${TEST_STUDIO_PATH}?tab=sets`]: 'nav-test-sets',
  [`#${TEST_STUDIO_PATH}?tab=executions`]: 'nav-test-executions',
};

const withInfoHotspot = (link: SideNavigationProps.Link, hotspotId: string): SideNavigationProps.Link => {
  const hotspot = React.createElement(TourHotspot, { hotspotId, side: 'right' });
  const info = link.info ? React.createElement(SpaceBetween, { direction: 'horizontal', size: 'xxs' }, link.info, hotspot) : hotspot;
  return { ...link, info };
};

const withNavHotspots = (items: readonly SideNavigationProps.Item[]): SideNavigationProps.Item[] =>
  items.map((item) => {
    if (item.type === 'section' && Array.isArray((item as SideNavigationProps.Section).items)) {
      const section = item as SideNavigationProps.Section;
      // The Extensions section, which Browse catalog leads.
      if (isSectionLedBy(section, BROWSE_CATALOG_HREF)) {
        const [first, ...rest] = section.items;
        const firstWithHotspot = first.type === 'link' ? withInfoHotspot(first as SideNavigationProps.Link, 'nav-extensions') : first;
        return { ...section, items: [firstWithHotspot, ...rest] };
      }
      return { ...section, items: withNavHotspots(section.items) };
    }
    if (item.type === 'link') {
      const link = item as SideNavigationProps.Link;
      const hotspotId = NAV_HOTSPOTS[link.href];
      if (hotspotId) {
        return withInfoHotspot(link, hotspotId);
      }
    }
    return item;
  });

type NavIntl = Pick<IntlShape, 'formatMessage'>;

// The role-specific items are built per render from `intl`, so every label is in the
// UI's language; the hrefs do not change with it.

// Full navigation items for Admin users (all features)
export const adminNavItems = (intl: NavIntl) => [
  {
    type: 'section',
    text: intl.formatMessage(messages.documentsSection),
    items: [
      { type: 'link', text: intl.formatMessage(messages.documentList), href: `#${DOCUMENTS_PATH}` },
      { type: 'link', text: intl.formatMessage(messages.uploadDocuments), href: `#${UPLOAD_DOCUMENT_PATH}` },
    ],
  },
  {
    type: 'section',
    text: intl.formatMessage(messages.assistantsSection),
    items: [
      { type: 'link', text: intl.formatMessage(messages.knowledgeBase), href: `#${DOCUMENTS_KB_QUERY_PATH}` },
      { type: 'link', text: intl.formatMessage(messages.agentChat), href: `#${AGENT_CHAT_PATH}` },
    ],
  },
  {
    type: 'section',
    text: intl.formatMessage(messages.testStudioSection),
    items: [
      { type: 'link', text: intl.formatMessage(messages.testSets), href: TEST_SETS_HREF },
      { type: 'link', text: intl.formatMessage(messages.testExecutions), href: `#${TEST_STUDIO_PATH}?tab=executions` },
    ],
  },
  {
    type: 'section',
    text: intl.formatMessage(messages.configurationSection),
    items: [
      { type: 'link', text: intl.formatMessage(messages.viewEditConfiguration), href: CONFIGURATION_HREF },
      { type: 'link', text: intl.formatMessage(messages.discovery), href: `#${DISCOVERY_PATH}` },
      { type: 'link', text: intl.formatMessage(messages.customModels), href: CUSTOM_MODELS_HREF },
      { type: 'link', text: intl.formatMessage(messages.capacityPlanning), href: CAPACITY_PLANNING_HREF },
      { type: 'link', text: intl.formatMessage(messages.userManagement), href: `#${USER_MANAGEMENT_PATH}` },
      // Admin-only, and only in this list: a mapping assigns a Configuration
      // Profile, which is the document-visibility partition, so it is not an
      // Author-level setting.
      { type: 'link', text: intl.formatMessage(messages.prefixMappings), href: `#${CONFIG_PREFIX_MAPPINGS_PATH}` },
      // Admin-only: MCP service credentials. Hidden on a stack without MCP.
      { type: 'link', text: intl.formatMessage(messages.mcpConfiguration), href: MCP_CONFIGURATION_HREF },
      { type: 'link', text: intl.formatMessage(messages.viewEditPricing), href: `#${PRICING_PATH}` },
      { type: 'link', text: intl.formatMessage(messages.viewEditModelLimits), href: `#${MODEL_CONFIG_LIMITS_PATH}` },
    ],
  },
  {
    type: 'section',
    text: intl.formatMessage(messages.resourcesSection),
    items: [
      {
        type: 'link',
        text: intl.formatMessage(messages.blog),
        href: BLOG_URL,
        external: true,
      },
      {
        type: 'link',
        text: intl.formatMessage(messages.documentation),
        href: 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/',
        external: true,
      },
      {
        type: 'link',
        text: 'README',
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/README.md',
        external: true,
      },
      {
        type: 'link',
        text: intl.formatMessage(messages.sourceCode),
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws',
        external: true,
      },
      {
        type: 'link',
        text: intl.formatMessage(messages.reportIssue),
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues',
        external: true,
      },
    ],
  },
];

// Author navigation: same as admin but without User Management or the Assistants section (Knowledge Base, Agent Chat) (not yet config-version scoped)
export const authorNavItems = (intl: NavIntl) => [
  {
    type: 'section',
    text: intl.formatMessage(messages.documentsSection),
    items: [
      { type: 'link', text: intl.formatMessage(messages.documentList), href: `#${DOCUMENTS_PATH}` },
      { type: 'link', text: intl.formatMessage(messages.uploadDocuments), href: `#${UPLOAD_DOCUMENT_PATH}` },
    ],
  },
  {
    type: 'section',
    text: intl.formatMessage(messages.testStudioSection),
    items: [
      { type: 'link', text: intl.formatMessage(messages.testSets), href: TEST_SETS_HREF },
      { type: 'link', text: intl.formatMessage(messages.testExecutions), href: `#${TEST_STUDIO_PATH}?tab=executions` },
    ],
  },
  {
    type: 'section',
    text: intl.formatMessage(messages.configurationSection),
    items: [
      { type: 'link', text: intl.formatMessage(messages.viewEditConfiguration), href: CONFIGURATION_HREF },
      { type: 'link', text: intl.formatMessage(messages.discovery), href: `#${DISCOVERY_PATH}` },
      { type: 'link', text: intl.formatMessage(messages.customModels), href: CUSTOM_MODELS_HREF },
      { type: 'link', text: intl.formatMessage(messages.capacityPlanning), href: CAPACITY_PLANNING_HREF },
      { type: 'link', text: intl.formatMessage(messages.viewPricing), href: `#${PRICING_PATH}` },
      { type: 'link', text: intl.formatMessage(messages.viewModelLimits), href: `#${MODEL_CONFIG_LIMITS_PATH}` },
    ],
  },
  {
    type: 'section',
    text: intl.formatMessage(messages.resourcesSection),
    items: [
      {
        type: 'link',
        text: intl.formatMessage(messages.blog),
        href: BLOG_URL,
        external: true,
      },
      {
        type: 'link',
        text: intl.formatMessage(messages.documentation),
        href: 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/',
        external: true,
      },
      {
        type: 'link',
        text: 'README',
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/README.md',
        external: true,
      },
      {
        type: 'link',
        text: intl.formatMessage(messages.sourceCode),
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws',
        external: true,
      },
      {
        type: 'link',
        text: intl.formatMessage(messages.reportIssue),
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues',
        external: true,
      },
    ],
  },
];

// Viewer navigation: read-only access to documents, config, capacity planning (no KB or Agent Chat — not yet config-version scoped)
export const viewerNavItems = (intl: NavIntl) => [
  {
    type: 'section',
    text: intl.formatMessage(messages.documentsSection),
    items: [{ type: 'link', text: intl.formatMessage(messages.documentList), href: `#${DOCUMENTS_PATH}` }],
  },
  {
    type: 'section',
    text: intl.formatMessage(messages.configurationSection),
    items: [
      { type: 'link', text: intl.formatMessage(messages.viewConfiguration), href: CONFIGURATION_HREF },
      { type: 'link', text: intl.formatMessage(messages.capacityPlanning), href: CAPACITY_PLANNING_HREF },
      { type: 'link', text: intl.formatMessage(messages.viewPricing), href: `#${PRICING_PATH}` },
      { type: 'link', text: intl.formatMessage(messages.viewModelLimits), href: `#${MODEL_CONFIG_LIMITS_PATH}` },
    ],
  },
  {
    type: 'section',
    text: intl.formatMessage(messages.resourcesSection),
    items: [
      {
        type: 'link',
        text: intl.formatMessage(messages.blog),
        href: BLOG_URL,
        external: true,
      },
      {
        type: 'link',
        text: intl.formatMessage(messages.documentation),
        href: 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/',
        external: true,
      },
      {
        type: 'link',
        text: 'README',
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/README.md',
        external: true,
      },
      {
        type: 'link',
        text: intl.formatMessage(messages.sourceCode),
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws',
        external: true,
      },
      {
        type: 'link',
        text: intl.formatMessage(messages.reportIssue),
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues',
        external: true,
      },
    ],
  },
];

// Keep for backward compatibility
export const documentsNavItems = adminNavItems;

const defaultOnFollowHandler = (ev: CustomEvent<SideNavigationProps.FollowDetail>): void => {
  // Prevent navigation for non-navigable items (deployment info, region-restricted features)
  const nonNavigableHrefs = [
    '#deployment-info',
    '#custom-models-unavailable',
    '#stackname',
    '#version',
    '#builddatetime',
    '#idppattern',
    '#region',
    '#update-available',
    COMING_SOON_HREF,
  ];
  if (nonNavigableHrefs.includes(ev.detail.href)) {
    ev.preventDefault();
    return;
  }
  console.log(ev);
};

interface NavigationProps {
  header?: { text: string; href: string };
  items?: SideNavigationProps.Item[];
  onFollowHandler?: (ev: CustomEvent<SideNavigationProps.FollowDetail>) => void;
  /**
   * The entry to highlight, set by a page whose URL does not say (the edit page, from its
   * mode). `null` highlights nothing; omitted, the entry is derived from the URL.
   */
  activeHref?: string | null;
}

/** The page's override, or the entry derived from the URL. */
const resolveActiveHref = (override: string | null | undefined, computed: string): string | undefined =>
  override === undefined ? computed : (override ?? undefined);

/**
 * Build the AWS CloudFormation "Update stack" deep-link for the current
 * deployment. The console pre-selects the stack and template URL so the
 * user lands on the parameters review page.
 *
 * Returns null if either piece of info is missing (e.g. headless deployments
 * where StackName isn't published into Settings).
 */
const buildCfnUpdateStackUrl = (region: string | undefined, stackName: string | undefined, templateUrl: string | null): string | null => {
  if (!region || !stackName || !templateUrl) return null;
  const encodedTemplate = encodeURIComponent(templateUrl);
  const encodedStack = encodeURIComponent(stackName);
  return (
    `https://${region}.console.aws.amazon.com/cloudformation/home` +
    `?region=${region}#/stacks/update/template` +
    `?stackId=${encodedStack}&templateURL=${encodedTemplate}`
  );
};

/**
 * The "Deployment Info" section (stack, version with its "Update" badge, build, pattern,
 * region), shared by every navigation. Null until any of it is known. The release check
 * it reads is open to every signed-in user; only the popover's CloudFormation link is
 * Admin's.
 */
const useDeploymentInfoNavSection = (): SideNavigationProps.Item | null => {
  const intl = useIntl();
  const { settings } = useSettingsContext();
  const { isAdmin } = useUserRole();
  // Version-check for the "Update available" indicator. Returns
  // isUpdateAvailable=false until settings.Version is loaded.
  const currentVersion = settings?.Version as string | undefined;
  const { latestVersion, templateUrl, isUpdateAvailable } = useLatestVersion(currentVersion);

  const deployedRegion = import.meta.env.VITE_AWS_REGION as string | undefined;
  const stackName = settings?.StackName as string | undefined;

  // Build the "Update available" popover content. Admin users get a clickable
  // CFN console deep-link; non-admins see a non-actionable hint pointing them
  // at their administrator. Both see the version diff.
  const cfnUpdateStackUrl = buildCfnUpdateStackUrl(deployedRegion, stackName, templateUrl);
  const updatePopoverContent =
    isAdmin && cfnUpdateStackUrl ? (
      <SpaceBetween size="xs">
        <Box>
          <FormattedMessage {...messages.latestVersion} values={{ version: latestVersion ?? '', strong }} />
          <br />
          <FormattedMessage {...messages.currentVersion} values={{ version: currentVersion ?? '', strong }} />
        </Box>
        <Link external href={cfnUpdateStackUrl}>
          <FormattedMessage {...messages.updateStackLink} />
        </Link>
        <Box variant="small" color="text-body-secondary">
          <FormattedMessage {...messages.updateStackHelp} />
        </Box>
      </SpaceBetween>
    ) : (
      <SpaceBetween size="xs">
        <Box>
          <FormattedMessage {...messages.latestVersion} values={{ version: latestVersion ?? '', strong }} />
          <br />
          <FormattedMessage {...messages.currentVersion} values={{ version: currentVersion ?? '', strong }} />
        </Box>
        <Box variant="small" color="text-body-secondary">
          <FormattedMessage {...messages.contactAdministrator} />
        </Box>
      </SpaceBetween>
    );

  if (settings?.Version || settings?.StackName || settings?.BuildDateTime || settings?.IDPPattern || deployedRegion) {
    const deploymentInfoItems: SideNavigationProps.Item[] = [];

    if (settings?.StackName) {
      deploymentInfoItems.push({
        type: 'link',
        text: intl.formatMessage(messages.stackName, { stackName: settings.StackName as string }),
        href: '#stackname',
      });
    }
    if (settings?.Version) {
      // Attach an "Update" badge + popover when the version-check resolver
      // has reported a newer published version. The badge itself is shown
      // to all roles; the popover content (Admin gets the actionable link)
      // is built above.
      const versionItem: SideNavigationProps.Link = {
        type: 'link',
        text: intl.formatMessage(messages.version, { version: settings.Version as string }),
        href: '#version',
      };
      if (isUpdateAvailable && latestVersion) {
        // info appears as a small adornment to the right of the link text.
        // Using a Popover with triggerType="custom" wrapping a Badge keeps
        // the styling consistent with the existing "us-east-1 only" badge.
        (versionItem as SideNavigationProps.Link & { info?: React.ReactNode }).info = (
          <Popover
            header={intl.formatMessage(messages.updateAvailable)}
            dismissButton={false}
            position="right"
            size="medium"
            triggerType="custom"
            content={updatePopoverContent}
          >
            <Badge color="blue">
              <FormattedMessage {...messages.updateBadge} />
            </Badge>
          </Popover>
        );
      }
      deploymentInfoItems.push(versionItem);
    }
    if (settings?.BuildDateTime) {
      deploymentInfoItems.push({
        type: 'link',
        text: intl.formatMessage(messages.build, { buildDateTime: settings.BuildDateTime as string }),
        href: '#builddatetime',
      });
    }
    if (settings?.IDPPattern) {
      const pattern = (settings.IDPPattern as string).split(' ')[0];
      deploymentInfoItems.push({ type: 'link', text: intl.formatMessage(messages.pattern, { pattern }), href: '#idppattern' });
    }
    if (deployedRegion) {
      deploymentInfoItems.push({ type: 'link', text: intl.formatMessage(messages.region, { region: deployedRegion }), href: '#region' });
    }

    return {
      type: 'section',
      text: intl.formatMessage(messages.deploymentInfoSection),
      items: deploymentInfoItems,
    } as SideNavigationProps.Item;
  }
  return null;
};

const FullNavigation = ({
  header,
  items,
  onFollowHandler = defaultOnFollowHandler,
  activeHref: pinned,
}: NavigationProps): React.JSX.Element => {
  const intl = useIntl();
  const location = useLocation();
  const path = location.pathname;
  let activeHref = `#${DEFAULT_PATH}`;
  const { settings } = useSettingsContext();
  const { loading: roleLoading, isAdmin, isAuthor, isViewerOnly, isQueueWorker, canReview } = useUserRole();
  const quickStartAvailable = useQuickStartAvailable();
  const reviewWork = useOptionalReviewWork();
  const reviewCount = reviewWork?.count ?? null;
  // Home (the Inbox) is everyone's; its Tasks and Queues are for Annotators, who are
  // assigned HITL work. Review Studio › Review Management is Admin-only.
  const showHitl = isAdmin;
  const { features: installedFeatures } = useInstalledFeatures();
  const { features: catalogFeatures } = useCatalogFeatures();
  const { unreadCount } = useInbox();

  // Dynamic "Extensions" section: installed features plus catalog features
  // with showInNav !== false, ending with a "Browse catalog" link. Always
  // visible per the feature platform plan. Catalog entries with
  // showInNav: false (the bundled reference samples) are reachable from the
  // catalog browser at /features, not from their own nav links.
  // isAdmin gates the badge wording only: installing is Admin-only, so a
  // non-admin reads lifecycle state rather than an action they cannot take.
  const featuresSection = useMemo(
    () => buildFeaturesNavSection(installedFeatures, catalogFeatures, isAdmin, intl),
    [installedFeatures, catalogFeatures, isAdmin, intl],
  );

  // Select navigation items based on user role (highest privilege wins)
  const baseItems = useMemo(() => {
    if (items) return items;
    let roleItems: Array<Record<string, unknown>>;
    if (isAdmin) roleItems = adminNavItems(intl);
    else if (isAuthor) roleItems = authorNavItems(intl);
    else if (isViewerOnly) roleItems = viewerNavItems(intl);
    else roleItems = viewerNavItems(intl); // Viewer, and Viewer + Reviewer (the union)

    // Home (the Inbox, plus an Annotator's Tasks and Queues) comes first for everyone.
    const home = buildHomeNavSection(buildInboxNavLink(unreadCount, intl), {
      showWork: isQueueWorker,
      taskCount: reviewCount,
      showJobs: !!canReview,
      intl,
    });
    roleItems = [home as unknown as Record<string, unknown>, ...roleItems];
    if (showHitl) {
      // HITL follows Test Studio; without Test Studio it goes before the trailing sections.
      const section = buildReviewStudioNavSection(intl) as unknown as Record<string, unknown>;
      const testStudioIdx = roleItems.findIndex((i) => isSectionLedBy(i, TEST_SETS_HREF));
      const resourcesIdx = roleItems.findIndex((i) => isSectionLedBy(i, BLOG_URL));
      const at = testStudioIdx >= 0 ? testStudioIdx + 1 : resourcesIdx >= 0 ? resourcesIdx : roleItems.length;
      roleItems = [...roleItems.slice(0, at), section, ...roleItems.slice(at)];
    }

    // Insert the dynamic Extensions section just before Resources
    // (or at the end if Resources isn't present).
    const resourcesIdx = roleItems.findIndex((i) => isSectionLedBy(i, BLOG_URL));
    if (resourcesIdx < 0) return [...roleItems, featuresSection as unknown as Record<string, unknown>];
    return [...roleItems.slice(0, resourcesIdx), featuresSection as unknown as Record<string, unknown>, ...roleItems.slice(resourcesIdx)];
  }, [items, isAdmin, isAuthor, isViewerOnly, featuresSection, unreadCount, isQueueWorker, showHitl, reviewCount, canReview, intl]);

  // Filter out navigation items based on deployment context:
  // - Capacity Planning: hidden if pattern is not Pattern-2 or Unified
  // - Custom Models: hidden if deployed region is not us-east-1
  const filteredItems = useMemo(() => {
    const pattern = (settings?.IDPPattern as string | undefined)?.toLowerCase();

    // Check for Pattern-2 or Unified in various formats
    const isCapacityPlanningSupported =
      !pattern || // Show if pattern not loaded yet (fail-safe)
      pattern.includes('pattern-2') ||
      pattern.includes('pattern 2') ||
      pattern.includes('pattern_2') ||
      pattern.includes('pattern2') ||
      pattern.includes('unified') ||
      /pattern[\s\-_]?2/.test(pattern); // Regex: "pattern" followed by optional separator, then "2"

    // Custom Models is only available in us-east-1 (Nova fine-tuning region requirement)
    const deployedRegion = import.meta.env.VITE_AWS_REGION as string | undefined;
    const isCustomModelsSupported = deployedRegion === 'us-east-1';

    // Build list of items to hide from the Configuration section, by href: the text
    // follows the UI's language, the href does not.
    const hiddenConfigItems = new Set<string>();
    if (!isCapacityPlanningSupported) {
      hiddenConfigItems.add(CAPACITY_PLANNING_HREF);
    }
    // MCP Configuration only where the stack has an MCP server. Hidden until the
    // settings say so, so a stack without MCP never shows a dead link. Keyed on the
    // link's href, which does not change with the language.
    const showMcpConfiguration = Boolean(settings?.McpServerEndpoint);

    // Transform navigation items: hide unsupported items, grey out region-restricted items
    return baseItems
      .map((rawItem) => {
        const item = rawItem as { type?: string; items?: unknown[] };
        if (isSectionLedBy(item, CONFIGURATION_HREF)) {
          const section = item as unknown as SideNavigationProps.Section;
          return {
            ...item,
            items: section.items
              .filter((subItem) => !hiddenConfigItems.has((subItem as { href?: string }).href ?? ''))
              .filter((subItem) => showMcpConfiguration || (subItem as { href?: string }).href !== MCP_CONFIGURATION_HREF)
              .map((subItem) => {
                const link = subItem as SideNavigationProps.Link;
                // Grey out Custom Models with region badge when not in us-east-1
                if (link.href === CUSTOM_MODELS_HREF && !isCustomModelsSupported) {
                  return {
                    ...link,
                    href: '#custom-models-unavailable',
                    info: React.createElement(
                      Popover,
                      {
                        dismissButton: false,
                        position: 'right',
                        size: 'medium',
                        triggerType: 'custom',
                        content: intl.formatMessage(messages.customModelsRegionNote),
                      },
                      React.createElement(Badge, { color: 'grey' }, intl.formatMessage(messages.customModelsRegionBadge)),
                    ),
                  };
                }
                return subItem;
              }),
          };
        }
        return item;
      })
      .filter((item) => !hiddenConfigItems.has((item as { href?: string }).href ?? ''));
  }, [baseItems, settings?.IDPPattern, settings?.McpServerEndpoint, intl]);

  // Determine active link based on current path
  if (path.includes(PRICING_PATH)) {
    activeHref = `#${PRICING_PATH}`;
  } else if (path.includes(MODEL_CONFIG_LIMITS_PATH)) {
    activeHref = `#${MODEL_CONFIG_LIMITS_PATH}`;
  } else if (path.includes(CONFIGURATION_PATH)) {
    activeHref = `#${CONFIGURATION_PATH}`;
  } else if (path.includes(DOCUMENTS_KB_QUERY_PATH)) {
    activeHref = `#${DOCUMENTS_KB_QUERY_PATH}`;
  } else if (path.includes(TEST_STUDIO_PATH) && path.includes('/annotate')) {
    // Test-set annotation is HITL work.
    activeHref = `#${REVIEW_QUEUES_PATH}`;
  } else if (path.includes(TEST_STUDIO_PATH)) {
    const urlParams = new URLSearchParams(location.search);
    const tab = urlParams.get('tab');
    activeHref = tab ? `#${TEST_STUDIO_PATH}?tab=${tab}` : `#${TEST_STUDIO_PATH}?tab=sets`;
  } else if (path.includes(UPLOAD_DOCUMENT_PATH)) {
    activeHref = `#${UPLOAD_DOCUMENT_PATH}`;
  } else if (path.includes(DISCOVERY_PATH)) {
    activeHref = `#${DISCOVERY_PATH}`;
  } else if (path.includes(USER_MANAGEMENT_PATH)) {
    activeHref = `#${USER_MANAGEMENT_PATH}`;
  } else if (path.includes(CONFIG_PREFIX_MAPPINGS_PATH)) {
    activeHref = `#${CONFIG_PREFIX_MAPPINGS_PATH}`;
  } else if (path.includes(MCP_CONFIGURATION_PATH)) {
    activeHref = MCP_CONFIGURATION_HREF;
  } else if (path.includes(CUSTOM_MODELS_PATH)) {
    activeHref = `#${CUSTOM_MODELS_PATH}`;
  } else if (path.includes(CAPACITY_PLANNING_PATH)) {
    activeHref = `#${CAPACITY_PLANNING_PATH}`;
  } else if (path.startsWith(MY_QUEUES_PATH)) {
    activeHref = `#${MY_QUEUES_PATH}`;
  } else if (path.startsWith(JOBS_PATH)) {
    activeHref = `#${JOBS_PATH}`;
  } else if (path.startsWith(MY_WORK_PATH)) {
    activeHref = `#${MY_TASKS_PATH}`;
  } else if (path.startsWith(REVIEW_QUEUES_PATH)) {
    activeHref = `#${REVIEW_QUEUES_PATH}`;
  } else if (path.startsWith(INBOX_PATH)) {
    activeHref = `#${INBOX_PATH}`;
  } else if (path.includes(DOCUMENTS_PATH)) {
    activeHref = `#${DOCUMENTS_PATH}`;
  } else if (path === AGENT_CHAT_PATH) {
    activeHref = `#${AGENT_CHAT_PATH}`;
  } else if (path.startsWith(FEATURES_PATH_PREFIX)) {
    activeHref = `#${path}`;
  }

  // Create navigation items with deployment info
  const navigationItems: SideNavigationProps.Item[] = [...filteredItems] as SideNavigationProps.Item[];

  const deploymentInfo = useDeploymentInfoNavSection();
  if (deploymentInfo) navigationItems.push(deploymentInfo);

  if (roleLoading && !items) {
    return (
      <div className="idp-side-nav">
        <SideNavigation items={[]} header={header} />
      </div>
    );
  }

  return (
    <div className="idp-side-nav">
      {/* Annotator-only and Reviewer-only users get QueueWorkerNavigation, which has no Quick Start. */}
      {quickStartAvailable && (
        <div className="nav-quick-start">
          <TourHotspot hotspotId="nav-quick-start" side="right">
            <Button variant="primary" iconName="gen-ai" onClick={() => window.dispatchEvent(new CustomEvent('openQuickStart'))}>
              <FormattedMessage {...messages.quickStart} />
            </Button>
          </TourHotspot>
        </div>
      )}
      <SideNavigation
        items={withNavHotspots(navigationItems)}
        header={header}
        activeHref={resolveActiveHref(pinned, activeHref)}
        onFollow={onFollowHandler}
      />
    </div>
  );
};

/**
 * Navigation for a user whose only work is the review queues — Annotator-only, or a
 * Reviewer with no Admin, Author or Viewer role: Home (Inbox, Tasks, Queues) and
 * Deployment Info. No Documents or Extensions: their work is reached from Home.
 * No Test Studio either: an Annotator reaches its test-set queues from Home › Queues,
 * and a Reviewer works Production queues only. Mounts no feature or catalog hooks.
 */
const QueueWorkerNavigation = ({ onFollowHandler = defaultOnFollowHandler, activeHref: pinned }: NavigationProps): React.JSX.Element => {
  const intl = useIntl();
  const location = useLocation();
  const path = location.pathname;
  const { unreadCount } = useInbox();
  const reviewWork = useOptionalReviewWork();
  const deploymentInfo = useDeploymentInfoNavSection();
  const { canReview } = useUserRole();
  const items: SideNavigationProps.Item[] = [
    buildHomeNavSection(buildInboxNavLink(unreadCount, intl), {
      showWork: true,
      taskCount: reviewWork?.count ?? null,
      showJobs: !!canReview,
      intl,
    }),
    ...(deploymentInfo ? [deploymentInfo] : []),
  ];
  // The edit pages (a Test Studio item under /review-queues/.../items for an Annotator, or a document's
  // edit page, which sets the entry itself) are opened from Tasks; a queue's own page
  // (under /review-queues) from Queues.
  let activeHref = `#${MY_TASKS_PATH}`;
  if (path.startsWith(INBOX_PATH)) activeHref = `#${INBOX_PATH}`;
  else if (path.startsWith(JOBS_PATH)) activeHref = `#${JOBS_PATH}`;
  else if (path.startsWith(MY_QUEUES_PATH) || (path.startsWith(REVIEW_QUEUES_PATH) && !path.includes('/items/')))
    activeHref = `#${MY_QUEUES_PATH}`;
  return (
    <div className="idp-side-nav">
      <SideNavigation items={items} activeHref={resolveActiveHref(pinned, activeHref)} onFollow={onFollowHandler} />
    </div>
  );
};

/** Picks the navigation for the caller's role; renders only a header until the role is known. */
const Navigation = (props: NavigationProps): React.JSX.Element => {
  const { loading, isAnnotatorOnly, isReviewerOnly } = useUserRole();
  if (loading) {
    return (
      <div className="idp-side-nav">
        <SideNavigation items={[]} header={props.header} />
      </div>
    );
  }
  if (isAnnotatorOnly || isReviewerOnly) return <QueueWorkerNavigation {...props} />;
  return <FullNavigation {...props} />;
};

export default Navigation;
