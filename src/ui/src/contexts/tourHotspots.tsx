// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { createContext, useContext, useEffect } from 'react';
import Hotspot, { type HotspotProps } from '@cloudscape-design/components/hotspot';

/** Records that a hotspot is on the page, and returns the call that forgets it. */
export type RegisterHotspot = (hotspotId: string) => () => void;

export const HotspotRegistry = createContext<RegisterHotspot | null>(null);

/**
 * A Cloudscape Hotspot the guided tour can see. A tour step whose hotspot is not on
 * the page cannot be shown, and Cloudscape then holds the tour on the step before
 * it, so the tour keeps only the steps whose hotspot is mounted. Render every
 * hotspot through this component so that it is counted.
 *
 * Kept apart from the tour provider so that the side navigation, which renders
 * these, draws in nothing that listens for events.
 */
export const TourHotspot = (props: HotspotProps): React.JSX.Element => {
  const register = useContext(HotspotRegistry);
  const { hotspotId } = props;
  useEffect(() => register?.(hotspotId), [register, hotspotId]);
  return <Hotspot {...props} />;
};
