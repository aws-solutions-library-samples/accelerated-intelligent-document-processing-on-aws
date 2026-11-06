// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useCallback, useEffect, useState } from 'react';

/**
 * Track an element's content width with a ResizeObserver.
 *
 * Returns a callback ref to attach and the width in CSS pixels, or 0 before the
 * first measurement and wherever ResizeObserver is unavailable (jsdom), so callers
 * must treat 0 as "unknown" rather than as a real width.
 */
const useElementWidth = <T extends HTMLElement>(): [(el: T | null) => void, number] => {
  const [element, setElement] = useState<T | null>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    if (!element || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0;
      setWidth(Math.round(w));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);

  const ref = useCallback((el: T | null) => setElement(el), []);
  return [ref, width];
};

export default useElementWidth;
