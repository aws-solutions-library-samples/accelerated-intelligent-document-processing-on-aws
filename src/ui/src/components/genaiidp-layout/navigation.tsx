// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useMemo } from 'react';
import { useLocation } from 'react-router-dom';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { Badge, Box, Button, Hotspot, Link, Popover, SideNavigation, SpaceBetween } from '@cloudscape-design/components';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import useInstalledFeatures from '../../hooks/use-installed-features';
import useCatalogFeatures from '../../hooks/use-catalog-features';
import { buildFeaturesNavSection, COMING_SOON_HREF } from './feature-platform-nav-items';
import useLatestVersion from '../../hooks/use-latest-version';
import './navigation.css';

const isQuickStartWidgetEnabled = (): boolean => {
  const flag = import.meta.env.VITE_ENABLE_QUICK_START_WIDGET;
  return flag === undefined || flag === 'true' || flag === true;
};

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
  AGENT_CHAT_PATH,
  CAPACITY_PLANNING_PATH,
  CUSTOM_MODELS_PATH,
  FEATURES_PATH_PREFIX,
  ANNOTATE_LANDING_PATH,
  testSetAnnotateHref,
} from '../../routes/constants';

export const documentsNavHeader = { text: 'Tools', href: `#${DEFAULT_PATH}` };

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
  [`#${PRICING_PATH}`]: 'nav-pricing',
  [`#${TEST_STUDIO_PATH}?tab=sets`]: 'nav-test-sets',
  [`#${TEST_STUDIO_PATH}?tab=executions`]: 'nav-test-executions',
};

const withInfoHotspot = (link: SideNavigationProps.Link, hotspotId: string): SideNavigationProps.Link => {
  const hotspot = React.createElement(Hotspot, { hotspotId, side: 'right' });
  const info = link.info ? React.createElement(SpaceBetween, { direction: 'horizontal', size: 'xxs' }, link.info, hotspot) : hotspot;
  return { ...link, info };
};

const withNavHotspots = (items: readonly SideNavigationProps.Item[]): SideNavigationProps.Item[] =>
  items.map((item) => {
    if (item.type === 'section' && Array.isArray((item as SideNavigationProps.Section).items)) {
      const section = item as SideNavigationProps.Section;
      if (section.text === 'Extensions' && section.items.length > 0) {
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

// Full navigation items for Admin users (all features)
export const adminNavItems = [
  { type: 'link', text: 'Document List', href: `#${DOCUMENTS_PATH}` },
  { type: 'link', text: 'Upload Document(s)', href: `#${UPLOAD_DOCUMENT_PATH}` },
  { type: 'link', text: 'Document KB', href: `#${DOCUMENTS_KB_QUERY_PATH}` },
  { type: 'link', text: 'Agent Companion Chat', href: `#${AGENT_CHAT_PATH}` },
  {
    type: 'section',
    text: 'Configuration',
    items: [
      { type: 'link', text: 'View/Edit Configuration', href: `#${CONFIGURATION_PATH}` },
      { type: 'link', text: 'Discovery', href: `#${DISCOVERY_PATH}` },
      { type: 'link', text: 'Custom Models', href: `#${CUSTOM_MODELS_PATH}` },
      { type: 'link', text: 'Capacity Planning', href: `#${CAPACITY_PLANNING_PATH}` },
      { type: 'link', text: 'User Management', href: `#${USER_MANAGEMENT_PATH}` },
      { type: 'link', text: 'View / Edit Pricing', href: `#${PRICING_PATH}` },
      { type: 'link', text: 'View / Edit Model Limits', href: `#${MODEL_CONFIG_LIMITS_PATH}` },
    ],
  },
  {
    type: 'section',
    text: 'Test Studio',
    items: [
      { type: 'link', text: 'Test Sets', href: `#${TEST_STUDIO_PATH}?tab=sets` },
      { type: 'link', text: 'Test Executions', href: `#${TEST_STUDIO_PATH}?tab=executions` },
    ],
  },
  {
    type: 'section',
    text: 'Resources',
    items: [
      {
        type: 'link',
        text: 'Blog',
        href: 'https://www.amazon.com/genai-idp-accelerator',
        external: true,
      },
      {
        type: 'link',
        text: 'Documentation',
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
        text: 'Source Code',
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws',
        external: true,
      },
      {
        type: 'link',
        text: 'Report an issue',
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues',
        external: true,
      },
    ],
  },
];

// Author navigation: same as admin but without User Management, Document KB, or Agent Chat (not yet config-version scoped)
export const authorNavItems = [
  { type: 'link', text: 'Document List', href: `#${DOCUMENTS_PATH}` },
  { type: 'link', text: 'Upload Document(s)', href: `#${UPLOAD_DOCUMENT_PATH}` },
  {
    type: 'section',
    text: 'Configuration',
    items: [
      { type: 'link', text: 'View/Edit Configuration', href: `#${CONFIGURATION_PATH}` },
      { type: 'link', text: 'Discovery', href: `#${DISCOVERY_PATH}` },
      { type: 'link', text: 'Custom Models', href: `#${CUSTOM_MODELS_PATH}` },
      { type: 'link', text: 'Capacity Planning', href: `#${CAPACITY_PLANNING_PATH}` },
      { type: 'link', text: 'View Pricing', href: `#${PRICING_PATH}` },
      { type: 'link', text: 'View Model Limits', href: `#${MODEL_CONFIG_LIMITS_PATH}` },
    ],
  },
  {
    type: 'section',
    text: 'Test Studio',
    items: [
      { type: 'link', text: 'Test Sets', href: `#${TEST_STUDIO_PATH}?tab=sets` },
      { type: 'link', text: 'Test Executions', href: `#${TEST_STUDIO_PATH}?tab=executions` },
    ],
  },
  {
    type: 'section',
    text: 'Resources',
    items: [
      {
        type: 'link',
        text: 'Blog',
        href: 'https://www.amazon.com/genai-idp-accelerator',
        external: true,
      },
      {
        type: 'link',
        text: 'Documentation',
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
        text: 'Source Code',
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws',
        external: true,
      },
      {
        type: 'link',
        text: 'Report an issue',
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues',
        external: true,
      },
    ],
  },
];

// Viewer navigation: read-only access to documents, config, capacity planning (no KB or Agent Chat — not yet config-version scoped)
export const viewerNavItems = [
  { type: 'link', text: 'Document List', href: `#${DOCUMENTS_PATH}` },
  {
    type: 'section',
    text: 'Configuration',
    items: [
      { type: 'link', text: 'View Configuration', href: `#${CONFIGURATION_PATH}` },
      { type: 'link', text: 'Capacity Planning', href: `#${CAPACITY_PLANNING_PATH}` },
      { type: 'link', text: 'View Pricing', href: `#${PRICING_PATH}` },
      { type: 'link', text: 'View Model Limits', href: `#${MODEL_CONFIG_LIMITS_PATH}` },
    ],
  },
  {
    type: 'section',
    text: 'Resources',
    items: [
      {
        type: 'link',
        text: 'Blog',
        href: 'https://www.amazon.com/genai-idp-accelerator',
        external: true,
      },
      {
        type: 'link',
        text: 'Documentation',
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
        text: 'Source Code',
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws',
        external: true,
      },
      {
        type: 'link',
        text: 'Report an issue',
        href: 'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues',
        external: true,
      },
    ],
  },
];

// Limited navigation items for Reviewer-only users (HITL review only)
export const reviewerNavItems = [{ type: 'link', text: 'Document List', href: `#${DOCUMENTS_PATH}` }];

/**
 * Navigation for Annotator-only users: their assigned test set's queue and nothing
 * else. Built from the user's allowedTestSets so a single-set annotator lands
 * straight in the queue rather than on a chooser. With several sets, or none
 * resolved yet, falls back to the queue landing page, which handles both cases.
 */
export const buildAnnotatorNavItems = (allowedTestSets: string[] | null): Array<Record<string, unknown>> => {
  const items: Array<Record<string, unknown>> = [];
  if (allowedTestSets && allowedTestSets.length === 1) {
    items.push({ type: 'link', text: 'My annotation queue', href: testSetAnnotateHref(allowedTestSets[0]) });
    // Kept even alongside the single-set shortcut: scope is fetched once on mount,
    // so an annotator assigned a second set mid-session needs a way to reach it
    // without reloading.
    items.push({ type: 'link', text: 'All my assignments', href: `#${ANNOTATE_LANDING_PATH}` });
    return items;
  }
  return [{ type: 'link', text: 'My annotation queues', href: `#${ANNOTATE_LANDING_PATH}` }];
};

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
}

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

const Navigation = ({
  header = documentsNavHeader,
  items,
  onFollowHandler = defaultOnFollowHandler,
}: NavigationProps): React.JSX.Element => {
  const location = useLocation();
  const path = location.pathname;
  let activeHref = `#${DEFAULT_PATH}`;
  const { settings } = useSettingsContext();
  const { isAdmin, isAuthor, isReviewerOnly, isAnnotatorOnly, isViewerOnly, allowedTestSets } = useUserRole();
  const { features: installedFeatures } = useInstalledFeatures();
  const { features: catalogFeatures } = useCatalogFeatures();

  // Dynamic "Extensions" section: installed features plus catalog features
  // with showInNav !== false, ending with a "Browse catalog" link. Always
  // visible per the feature platform plan. Catalog entries with
  // showInNav: false (the bundled reference samples) are reachable from the
  // catalog browser at /features, not from their own nav links.
  // isAdmin gates the badge wording only: installing is Admin-only, so a
  // non-admin reads lifecycle state rather than an action they cannot take.
  const featuresSection = useMemo(
    () => buildFeaturesNavSection(installedFeatures, catalogFeatures, isAdmin),
    [installedFeatures, catalogFeatures, isAdmin],
  );

  // Version-check for the "Update available" indicator. Returns
  // isUpdateAvailable=false until settings.Version is loaded.
  const currentVersion = settings?.Version as string | undefined;
  const { latestVersion, templateUrl, isUpdateAvailable } = useLatestVersion(currentVersion);

  // Select navigation items based on user role (highest privilege wins)
  const baseItems = useMemo(() => {
    if (items) return items;
    let roleItems: Array<Record<string, unknown>>;
    if (isAdmin) roleItems = adminNavItems;
    else if (isAuthor) roleItems = authorNavItems;
    else if (isViewerOnly) roleItems = viewerNavItems;
    else if (isReviewerOnly) roleItems = reviewerNavItems;
    // Before the viewer fallback: an Annotator-only user must land in their queue,
    // not on a document list they cannot read.
    else if (isAnnotatorOnly) roleItems = buildAnnotatorNavItems(allowedTestSets);
    else roleItems = viewerNavItems; // Default: if user has Viewer + Reviewer, show viewer nav (union)

    // Insert the dynamic Extensions section just before Resources
    // (or at the end if Resources isn't present, e.g. reviewer nav).
    const resourcesIdx = roleItems.findIndex(
      (i) => (i as { type?: string }).type === 'section' && (i as { text?: string }).text === 'Resources',
    );
    if (resourcesIdx < 0) return [...roleItems, featuresSection as unknown as Record<string, unknown>];
    return [...roleItems.slice(0, resourcesIdx), featuresSection as unknown as Record<string, unknown>, ...roleItems.slice(resourcesIdx)];
  }, [items, isAdmin, isAuthor, isViewerOnly, isReviewerOnly, isAnnotatorOnly, allowedTestSets, featuresSection]);

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

    // Build list of items to hide from the Configuration section
    const hiddenConfigItems = new Set<string>();
    if (!isCapacityPlanningSupported) {
      hiddenConfigItems.add('Capacity Planning');
    }

    // Transform navigation items: hide unsupported items, grey out region-restricted items
    return baseItems
      .map((rawItem) => {
        const item = rawItem as { type?: string; text?: string; items?: unknown[] };
        if (item.type === 'section' && item.text === 'Configuration') {
          const section = item as unknown as SideNavigationProps.Section;
          return {
            ...item,
            items: section.items
              .filter((subItem) => !hiddenConfigItems.has((subItem as { text?: string }).text ?? ''))
              .map((subItem) => {
                const link = subItem as SideNavigationProps.Link;
                // Grey out Custom Models with region badge when not in us-east-1
                if (link.text === 'Custom Models' && !isCustomModelsSupported) {
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
                        content:
                          'Custom Models requires Amazon Nova fine-tuning, which is currently available in us-east-1 only. Deploy your stack in us-east-1 to use this feature.',
                      },
                      React.createElement(Badge, { color: 'grey' }, 'us-east-1 only'),
                    ),
                  };
                }
                return subItem;
              }),
          };
        }
        return item;
      })
      .filter((item) => !hiddenConfigItems.has((item as { text?: string }).text ?? ''));
  }, [baseItems, settings?.IDPPattern]);

  // Determine active link based on current path
  if (path.includes(PRICING_PATH)) {
    activeHref = `#${PRICING_PATH}`;
  } else if (path.includes(MODEL_CONFIG_LIMITS_PATH)) {
    activeHref = `#${MODEL_CONFIG_LIMITS_PATH}`;
  } else if (path.includes(CONFIGURATION_PATH)) {
    activeHref = `#${CONFIGURATION_PATH}`;
  } else if (path.includes(DOCUMENTS_KB_QUERY_PATH)) {
    activeHref = `#${DOCUMENTS_KB_QUERY_PATH}`;
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
  } else if (path.includes(CUSTOM_MODELS_PATH)) {
    activeHref = `#${CUSTOM_MODELS_PATH}`;
  } else if (path.includes(CAPACITY_PLANNING_PATH)) {
    activeHref = `#${CAPACITY_PLANNING_PATH}`;
  } else if (path.includes(DOCUMENTS_PATH)) {
    activeHref = `#${DOCUMENTS_PATH}`;
  } else if (path === AGENT_CHAT_PATH) {
    activeHref = `#${AGENT_CHAT_PATH}`;
  } else if (path.startsWith(FEATURES_PATH_PREFIX)) {
    activeHref = `#${path}`;
  }

  // Create navigation items with deployment info
  const navigationItems: SideNavigationProps.Item[] = [...filteredItems] as SideNavigationProps.Item[];

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
          Latest published version: <strong>v{latestVersion}</strong>
          <br />
          Currently deployed: <strong>v{currentVersion}</strong>
        </Box>
        <Link external href={cfnUpdateStackUrl}>
          Update stack in CloudFormation →
        </Link>
        <Box variant="small" color="text-body-secondary">
          Opens the AWS CloudFormation console with the new template URL pre-filled. Review parameters before applying.
        </Box>
      </SpaceBetween>
    ) : (
      <SpaceBetween size="xs">
        <Box>
          Latest published version: <strong>v{latestVersion}</strong>
          <br />
          Currently deployed: <strong>v{currentVersion}</strong>
        </Box>
        <Box variant="small" color="text-body-secondary">
          Contact your administrator to update this deployment to the latest version.
        </Box>
      </SpaceBetween>
    );

  if (settings?.Version || settings?.StackName || settings?.BuildDateTime || settings?.IDPPattern || deployedRegion) {
    const deploymentInfoItems: SideNavigationProps.Item[] = [];

    if (settings?.StackName) {
      deploymentInfoItems.push({ type: 'link', text: `Stack Name: ${settings.StackName}`, href: '#stackname' });
    }
    if (settings?.Version) {
      // Attach an "Update" badge + popover when the version-check resolver
      // has reported a newer published version. The badge itself is shown
      // to all roles; the popover content (Admin gets the actionable link)
      // is built above.
      const versionItem: SideNavigationProps.Link = {
        type: 'link',
        text: `Version: ${settings.Version}`,
        href: '#version',
      };
      if (isUpdateAvailable && latestVersion) {
        // info appears as a small adornment to the right of the link text.
        // Using a Popover with triggerType="custom" wrapping a Badge keeps
        // the styling consistent with the existing "us-east-1 only" badge.
        (versionItem as SideNavigationProps.Link & { info?: React.ReactNode }).info = (
          <Popover
            header="Update available"
            dismissButton={false}
            position="right"
            size="medium"
            triggerType="custom"
            content={updatePopoverContent}
          >
            <Badge color="blue">Update</Badge>
          </Popover>
        );
      }
      deploymentInfoItems.push(versionItem);
    }
    if (settings?.BuildDateTime) {
      deploymentInfoItems.push({ type: 'link', text: `Build: ${settings.BuildDateTime}`, href: '#builddatetime' });
    }
    if (settings?.IDPPattern) {
      const pattern = (settings.IDPPattern as string).split(' ')[0];
      deploymentInfoItems.push({ type: 'link', text: `Pattern: ${pattern}`, href: '#idppattern' });
    }
    if (deployedRegion) {
      deploymentInfoItems.push({ type: 'link', text: `Region: ${deployedRegion}`, href: '#region' });
    }

    navigationItems.push({
      type: 'section',
      text: 'Deployment Info',
      items: deploymentInfoItems,
    } as SideNavigationProps.Item);
  }

  return (
    <div className="idp-side-nav">
      {/* Quick Start drives discovery and document upload, outside what an
          Annotator is scoped to. Hidden rather than left to fail server-side, so the
          UI does not advertise a capability the role does not have. */}
      {isQuickStartWidgetEnabled() && !isAnnotatorOnly && (
        <div className="nav-quick-start">
          <Hotspot hotspotId="nav-quick-start" side="right">
            <Button variant="primary" iconName="gen-ai" onClick={() => window.dispatchEvent(new CustomEvent('openQuickStart'))}>
              Quick Start
            </Button>
          </Hotspot>
        </div>
      )}
      <SideNavigation
        items={withNavHotspots(navigationItems)}
        header={header || documentsNavHeader}
        activeHref={activeHref}
        onFollow={onFollowHandler}
      />
    </div>
  );
};

export default Navigation;
