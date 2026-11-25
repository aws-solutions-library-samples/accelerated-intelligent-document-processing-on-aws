// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useLocation } from 'react-router-dom';
import { Button, Icon } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import { AgentChatProvider, useAgentChatContext } from '../../contexts/agentChat';
import AgentChatLayout from './AgentChatLayout';
import { WELCOME_PATH } from '../../routes/constants';
import { isQuickStartEnabled } from '../../utils/quick-start-flag';
import { TourHotspot } from '../../contexts/tourHotspots';
import './QuickStartWidget.css';

const messages = defineMessages<{
  titleQuickStart: NoMessageValues;
  titleChat: NoMessageValues;
  openAriaLabel: NoMessageValues;
  dialogAriaLabel: NoMessageValues;
  minimize: NoMessageValues;
  close: NoMessageValues;
  resize: NoMessageValues;
}>({
  titleQuickStart: {
    id: 'agentChat.quickStartWidget.titleQuickStart',
    defaultMessage: 'Quick Start',
    description: 'Title of the floating assistant panel while it runs Quick Start, which drafts a configuration for a document type.',
  },
  titleChat: {
    id: 'agentChat.quickStartWidget.titleChat',
    defaultMessage: 'Agent Companion Chat',
    description:
      'Title of the floating assistant panel while it runs the Agent Companion Chat. Agent Companion Chat is the name of the chat feature.',
  },
  openAriaLabel: {
    id: 'agentChat.quickStartWidget.openAriaLabel',
    defaultMessage: 'Open Quick Start',
    description: 'Screen-reader label of the round launcher button that opens the floating Quick Start panel.',
  },
  dialogAriaLabel: {
    id: 'agentChat.quickStartWidget.dialogAriaLabel',
    defaultMessage: 'Assistant',
    description: 'Screen-reader name of the floating assistant panel that holds Quick Start and the Agent Companion Chat.',
  },
  minimize: {
    id: 'agentChat.quickStartWidget.minimize',
    defaultMessage: 'Minimize',
    description: 'Screen-reader label of the button that minimizes the floating assistant panel.',
  },
  close: {
    id: 'agentChat.quickStartWidget.close',
    defaultMessage: 'Close',
    description: 'Screen-reader label of the button that closes the floating assistant panel.',
  },
  resize: {
    id: 'agentChat.quickStartWidget.resize',
    defaultMessage: 'Resize',
    description: 'Screen-reader label of the corner handle that resizes the floating assistant panel.',
  },
});

const WidgetHeaderTitle = (): React.JSX.Element => {
  const { agentChatState } = useAgentChatContext();
  return (
    <span className="quick-start-widget-title">
      <FormattedMessage {...(agentChatState.mode === 'quick_start' ? messages.titleQuickStart : messages.titleChat)} />
    </span>
  );
};

const DRAG_THRESHOLD = 4;
const MARGIN = 24;
const PANEL_MAX_W = 550;
const PANEL_MAX_H = 680;
const PANEL_MIN_W = 360;
const PANEL_MIN_H = 420;
const LAUNCHER_SIZE = 56;

type View = 'closed' | 'open' | 'minimized';
type Corner = 'tl' | 'tr' | 'bl' | 'br';

interface Pin {
  corner: Corner;
  dx: number;
  dy: number;
}

interface AbsPos {
  x: number;
  y: number;
}

interface Size {
  w: number;
  h: number;
}

const PIN_KEY = 'idp-qs-widget-pin';
const SIZE_KEY = 'idp-qs-widget-size';

const loadSize = (): Size | null => {
  try {
    const raw = localStorage.getItem(SIZE_KEY);
    return raw ? (JSON.parse(raw) as Size) : null;
  } catch {
    return null;
  }
};

const DEFAULT_PIN: Pin = { corner: 'br', dx: MARGIN, dy: MARGIN };

const loadPin = (key: string): Pin | null => {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as Pin) : null;
  } catch {
    return null;
  }
};

const savePin = (key: string, pin: Pin): void => {
  try {
    localStorage.setItem(key, JSON.stringify(pin));
  } catch {
    /* ignore */
  }
};

const toPin = (x: number, y: number, w: number, h: number): Pin => {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const cx = x + w / 2;
  const cy = y + h / 2;
  const right = cx >= vw / 2;
  const bottom = cy >= vh / 2;
  const corner = `${bottom ? 'b' : 't'}${right ? 'r' : 'l'}` as Corner;
  const dx = right ? Math.max(8, vw - (x + w)) : Math.max(8, x);
  const dy = bottom ? Math.max(8, vh - (y + h)) : Math.max(8, y);
  return { corner, dx, dy };
};

const pinToStyle = (pin: Pin): React.CSSProperties => {
  const style: React.CSSProperties = { left: 'auto', right: 'auto', top: 'auto', bottom: 'auto' };
  if (pin.corner[0] === 't') style.top = pin.dy;
  else style.bottom = pin.dy;
  if (pin.corner[1] === 'l') style.left = pin.dx;
  else style.right = pin.dx;
  return style;
};

const QuickStartWidget = (): React.JSX.Element | null => {
  const intl = useIntl();
  const location = useLocation();
  const [view, setView] = useState<View>('closed');
  const [mounted, setMounted] = useState(false);

  const [pin, setPin] = useState<Pin>(() => loadPin(PIN_KEY) ?? DEFAULT_PIN);
  const [dragPos, setDragPos] = useState<AbsPos | null>(null);
  const [size, setSize] = useState<Size>(() => loadSize() ?? { w: PANEL_MAX_W, h: PANEL_MAX_H });

  const panelRef = useRef<HTMLDivElement>(null);
  const launcherRef = useRef<HTMLDivElement>(null);
  const dragState = useRef<{
    kind: 'panel' | 'launcher';
    startX: number;
    startY: number;
    originX: number;
    originY: number;
    w: number;
    h: number;
    moved: boolean;
  } | null>(null);
  const suppressNextClick = useRef(false);
  const resizeState = useRef<{ startX: number; startY: number; startW: number; startH: number; signX: number; signY: number } | null>(null);

  const doOpen = useCallback(() => {
    if (!mounted) {
      setMounted(true);
    }
    setView('open');
  }, [mounted]);

  useEffect(() => {
    const handleOpen = () => doOpen();
    window.addEventListener('openQuickStart', handleOpen);
    return () => window.removeEventListener('openQuickStart', handleOpen);
  }, [doOpen]);

  const clampAbs = useCallback((x: number, y: number, w: number, h: number): AbsPos => {
    return {
      x: Math.max(8, Math.min(x, window.innerWidth - w - 8)),
      y: Math.max(8, Math.min(y, window.innerHeight - h - 8)),
    };
  }, []);

  const onPointerMove = useCallback(
    (e: PointerEvent) => {
      const ds = dragState.current;
      if (!ds) return;
      const dx = e.clientX - ds.startX;
      const dy = e.clientY - ds.startY;
      if (!ds.moved && Math.abs(dx) + Math.abs(dy) < DRAG_THRESHOLD) return;
      ds.moved = true;
      setDragPos(clampAbs(ds.originX + dx, ds.originY + dy, ds.w, ds.h));
    },
    [clampAbs],
  );

  const onPointerUp = useCallback(() => {
    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('pointerup', onPointerUp);
    const ds = dragState.current;
    dragState.current = null;
    if (ds?.moved) {
      suppressNextClick.current = true;
      setDragPos((prev) => {
        if (prev) {
          const next = toPin(prev.x, prev.y, ds.w, ds.h);
          setPin(next);
          savePin(PIN_KEY, next);
        }
        return null;
      });
    } else {
      setDragPos(null);
    }
  }, [onPointerMove]);

  const startDrag = useCallback(
    (kind: 'panel' | 'launcher', ref: React.RefObject<HTMLElement | null>) => (e: React.PointerEvent) => {
      if (kind === 'panel' && (e.target as HTMLElement).closest('button')) return;
      const rect = ref.current?.getBoundingClientRect();
      dragState.current = {
        kind,
        startX: e.clientX,
        startY: e.clientY,
        originX: rect?.left ?? 0,
        originY: rect?.top ?? 0,
        w: rect?.width ?? (kind === 'panel' ? PANEL_MAX_W : LAUNCHER_SIZE),
        h: rect?.height ?? (kind === 'panel' ? PANEL_MAX_H : LAUNCHER_SIZE),
        moved: false,
      };
      window.addEventListener('pointermove', onPointerMove);
      window.addEventListener('pointerup', onPointerUp);
    },
    [onPointerMove, onPointerUp],
  );

  const onResizeMove = useCallback((e: PointerEvent) => {
    const rs = resizeState.current;
    if (!rs) return;
    const w = Math.max(PANEL_MIN_W, Math.min(rs.startW + rs.signX * (e.clientX - rs.startX), window.innerWidth - 16));
    const h = Math.max(PANEL_MIN_H, Math.min(rs.startH + rs.signY * (e.clientY - rs.startY), window.innerHeight - 16));
    setSize({ w, h });
  }, []);

  const onResizeUp = useCallback(() => {
    window.removeEventListener('pointermove', onResizeMove);
    window.removeEventListener('pointerup', onResizeUp);
    resizeState.current = null;
    setSize((prev) => {
      try {
        localStorage.setItem(SIZE_KEY, JSON.stringify(prev));
      } catch {
        /* ignore */
      }
      return prev;
    });
  }, [onResizeMove]);

  const startResize = useCallback(
    (e: React.PointerEvent) => {
      e.stopPropagation();
      const rect = panelRef.current?.getBoundingClientRect();
      resizeState.current = {
        startX: e.clientX,
        startY: e.clientY,
        startW: rect?.width ?? size.w,
        startH: rect?.height ?? size.h,
        signX: pin.corner[1] === 'l' ? 1 : -1,
        signY: pin.corner[0] === 't' ? 1 : -1,
      };
      window.addEventListener('pointermove', onResizeMove);
      window.addEventListener('pointerup', onResizeUp);
    },
    [onResizeMove, onResizeUp, pin, size],
  );

  const handleLauncherClick = useCallback(() => {
    if (suppressNextClick.current) {
      suppressNextClick.current = false;
      return;
    }
    doOpen();
  }, [doOpen]);

  const handleClose = useCallback(() => {
    setView('closed');
    setMounted(false);
  }, []);

  if (!isQuickStartEnabled() || location.pathname === WELCOME_PATH) {
    return null;
  }

  const dragStyle = dragPos ? { left: dragPos.x, top: dragPos.y, right: 'auto' as const, bottom: 'auto' as const } : undefined;

  const launcherWrapStyle: React.CSSProperties = {
    ...(dragState.current?.kind === 'launcher' && dragStyle ? dragStyle : pinToStyle(pin)),
    display: view === 'minimized' ? undefined : 'none',
  };

  const panelStyle: React.CSSProperties = {
    ...(dragState.current?.kind === 'panel' && dragStyle ? dragStyle : pinToStyle(pin)),
    display: view === 'open' ? undefined : 'none',
    width: size.w,
    height: size.h,
  };

  const handleCorner = `${pin.corner[0] === 't' ? 'b' : 't'}${pin.corner[1] === 'l' ? 'r' : 'l'}`;

  return (
    <div className="quick-start-widget">
      <div ref={launcherRef} className="quick-start-widget-launcher-wrap" style={launcherWrapStyle}>
        <TourHotspot hotspotId="quick-start-launcher" side="left">
          <button
            type="button"
            className="quick-start-widget-launcher"
            aria-label={intl.formatMessage(messages.openAriaLabel)}
            onPointerDown={startDrag('launcher', launcherRef)}
            onClick={handleLauncherClick}
          >
            <Icon name="gen-ai" size="medium" />
          </button>
        </TourHotspot>
      </div>
      <div
        ref={panelRef}
        className="quick-start-widget-panel"
        role="dialog"
        aria-label={intl.formatMessage(messages.dialogAriaLabel)}
        style={panelStyle}
      >
        {mounted && (
          <AgentChatProvider initialMode="quick_start">
            <div className="quick-start-widget-header" onPointerDown={startDrag('panel', panelRef)}>
              <WidgetHeaderTitle />
              <span className="quick-start-widget-header-actions">
                <Button
                  variant="icon"
                  iconName="treeview-collapse"
                  ariaLabel={intl.formatMessage(messages.minimize)}
                  onClick={() => setView('minimized')}
                />
                <Button variant="icon" iconName="close" ariaLabel={intl.formatMessage(messages.close)} onClick={handleClose} />
              </span>
            </div>
            <div className="quick-start-widget-chat">
              <AgentChatLayout showHeader={false} />
            </div>
            <span
              className={`quick-start-widget-resize quick-start-widget-resize-${handleCorner}`}
              onPointerDown={startResize}
              role="separator"
              aria-label={intl.formatMessage(messages.resize)}
            />
          </AgentChatProvider>
        )}
      </div>
    </div>
  );
};

export default QuickStartWidget;
