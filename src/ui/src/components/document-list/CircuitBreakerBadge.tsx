// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState } from 'react';
import { Button, Popover } from '@cloudscape-design/components';
import type { IconProps } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import useCircuitBreaker from '../../hooks/use-circuit-breaker';
import CircuitBreakerPanel from '../common/CircuitBreakerPanel';

const messages = defineMessages<{
  closed: NoMessageValues;
  recovering: NoMessageValues;
  manuallyPaused: NoMessageValues;
  bedrockOutage: NoMessageValues;
}>({
  closed: {
    id: 'documents.list.circuit.closed',
    defaultMessage: 'Circuit: closed',
    description: 'Badge in the document list header when the Bedrock circuit breaker is closed (processing runs normally).',
  },
  recovering: {
    id: 'documents.list.circuit.recovering',
    defaultMessage: 'Circuit: recovering',
    description: 'Badge in the document list header when the Bedrock circuit breaker is half-open (probing whether Bedrock is back).',
  },
  manuallyPaused: {
    id: 'documents.list.circuit.manuallyPaused',
    defaultMessage: 'Circuit: manually paused',
    description: 'Badge in the document list header when an Admin paused the Bedrock circuit breaker.',
  },
  bedrockOutage: {
    id: 'documents.list.circuit.bedrockOutage',
    defaultMessage: 'Circuit: Bedrock outage',
    description: 'Badge in the document list header when the Bedrock circuit breaker opened on failures.',
  },
});

interface StateDisplay {
  iconName: IconProps.Name;
  label: TypedMessageDescriptor<NoMessageValues>;
}

const MANUAL_PAUSE_PREFIX = 'Manual pause by';

const CircuitBreakerBadge = (): React.JSX.Element | null => {
  const intl = useIntl();
  const { status, loading, error, pause, resume, probe } = useCircuitBreaker();
  const [panelVisible, setPanelVisible] = useState<boolean>(false);

  if (loading || error || !status || !status.enabled || !status.state) {
    return null;
  }

  const isManual = status.lastError?.startsWith(MANUAL_PAUSE_PREFIX);

  const stateDisplay: Record<string, StateDisplay> = {
    CLOSED: { iconName: 'status-positive', label: messages.closed },
    HALF_OPEN: { iconName: 'status-in-progress', label: messages.recovering },
    OPEN: {
      iconName: 'status-stopped',
      label: isManual ? messages.manuallyPaused : messages.bedrockOutage,
    },
  };

  const display = stateDisplay[status.state];
  if (!display) return null;
  const label = intl.formatMessage(display.label);

  const button = (
    <Button iconName={display.iconName} variant="normal" onClick={() => setPanelVisible(true)} ariaLabel={label}>
      {label}
    </Button>
  );

  const showErrorPopover = status.state === 'OPEN' && !!status.lastError;

  return (
    <>
      {showErrorPopover ? (
        <Popover dismissButton={false} position="bottom" size="medium" triggerType="custom" content={status.lastError}>
          {button}
        </Popover>
      ) : (
        button
      )}
      <CircuitBreakerPanel
        visible={panelVisible}
        status={status}
        onDismiss={() => setPanelVisible(false)}
        onPause={pause}
        onResume={resume}
        onProbe={probe}
      />
    </>
  );
};

export default CircuitBreakerBadge;
