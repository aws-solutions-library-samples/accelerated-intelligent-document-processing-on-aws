// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect, useRef, memo, useCallback, useImperativeHandle } from 'react';
import { Box, Spinner, Button } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import generateS3PresignedUrl from './generate-s3-presigned-url';
import useAppContext from '../../contexts/app';
import { useDocumentVersion } from '../../contexts/document-version';

const logger = new ConsoleLogger('PageImageViewer');

interface BoundingBoxGeometry {
  boundingBox?: {
    left: number;
    top: number;
    width: number;
    height: number;
  };
  page?: number;
}

interface BoundingBoxProps {
  box: BoundingBoxGeometry | null;
  page: string | null;
  currentPage: string | null;
  imageRef: React.RefObject<HTMLImageElement | null>;
  containerRef?: React.RefObject<HTMLDivElement | null>;
  zoomLevel?: number;
  panOffset?: { x: number; y: number };
  color?: string;
  label?: string | null;
}

export const BoundingBox = memo(
  ({
    box,
    page,
    currentPage,
    imageRef,
    containerRef,
    zoomLevel = 1,
    panOffset = { x: 0, y: 0 },
    color = 'red',
    label = null,
  }: BoundingBoxProps): React.JSX.Element | null => {
    const [dimensions, setDimensions] = useState<Record<string, number>>({ width: 0, height: 0 });

    useEffect(() => {
      if (imageRef.current && page === currentPage) {
        const updateDimensions = () => {
          const img = imageRef.current;
          if (!img) return;
          const rect = img.getBoundingClientRect();
          // Use containerRef if provided, otherwise fall back to parentElement
          const container = containerRef?.current || img.parentElement;
          if (!container) return;
          const containerRect = container.getBoundingClientRect();

          // Get the actual displayed dimensions and position after all transforms
          const transformedWidth = rect.width;
          const transformedHeight = rect.height;
          const transformedOffsetX = rect.left - containerRect.left;
          const transformedOffsetY = rect.top - containerRect.top;

          setDimensions({
            transformedWidth,
            transformedHeight,
            transformedOffsetX,
            transformedOffsetY,
          });

          logger.debug('PageImageViewer - BoundingBox dimensions updated:', {
            imageWidth: img.width,
            imageHeight: img.height,
            naturalWidth: img.naturalWidth,
            naturalHeight: img.naturalHeight,
            offsetX: rect.left - containerRect.left,
            offsetY: rect.top - containerRect.top,
            imageRect: rect,
            containerRect,
          });
        };

        if (imageRef.current.complete && imageRef.current.naturalWidth > 0) {
          updateDimensions();
        } else {
          imageRef.current.addEventListener('load', updateDimensions);
        }

        return () => {
          if (imageRef.current) {
            imageRef.current.removeEventListener('load', updateDimensions);
          }
        };
      }
      return undefined;
    }, [imageRef, page, currentPage]);

    // Update dimensions when zoom or pan changes
    useEffect(() => {
      if (imageRef.current && page === currentPage) {
        const updateDimensions = () => {
          const img = imageRef.current;
          if (!img) return;
          const rect = img.getBoundingClientRect();
          // Use containerRef if provided, otherwise fall back to parentElement
          const container = containerRef?.current || img.parentElement;
          if (!container) return;
          const containerRect = container.getBoundingClientRect();

          const transformedWidth = rect.width;
          const transformedHeight = rect.height;
          const transformedOffsetX = rect.left - containerRect.left;
          const transformedOffsetY = rect.top - containerRect.top;

          setDimensions({
            transformedWidth,
            transformedHeight,
            transformedOffsetX,
            transformedOffsetY,
          });
        };
        // Delay to allow transforms to complete
        const timeoutId = setTimeout(updateDimensions, 150);
        // Ensure accuracy after reset
        const secondTimeoutId = setTimeout(updateDimensions, 300);
        return () => {
          clearTimeout(timeoutId);
          clearTimeout(secondTimeoutId);
        };
      }
      return undefined;
    }, [zoomLevel, panOffset, imageRef, containerRef, page, currentPage]);

    if (page !== currentPage || !box || !dimensions.transformedWidth) {
      return null;
    }

    if (!box.boundingBox) {
      return null;
    }

    const padding = 5;
    const bbox = box.boundingBox;

    // Calculate position and size directly on the transformed image
    const finalLeft = bbox.left * dimensions.transformedWidth + dimensions.transformedOffsetX - padding;
    const finalTop = bbox.top * dimensions.transformedHeight + dimensions.transformedOffsetY - padding;
    const finalWidth = bbox.width * dimensions.transformedWidth + padding * 2;
    const finalHeight = bbox.height * dimensions.transformedHeight + padding * 2;

    logger.debug('PageImageViewer - BoundingBox style calculated:', {
      bbox,
      dimensions,
      finalLeft,
      finalTop,
      finalWidth,
      finalHeight,
    });

    // Position the bounding box directly without additional transforms
    const style: React.CSSProperties = {
      position: 'absolute',
      left: `${finalLeft}px`,
      top: `${finalTop}px`,
      width: `${finalWidth}px`,
      height: `${finalHeight}px`,
      border: `2px solid ${color}`,
      pointerEvents: 'none',
      zIndex: 10,
      transition: 'all 0.1s ease-out',
    };

    return (
      <div style={style}>
        {label && (
          <div
            style={{
              position: 'absolute',
              top: '-20px',
              left: '0',
              backgroundColor: color,
              color: 'white',
              fontSize: '10px',
              padding: '2px 4px',
              borderRadius: '2px',
              whiteSpace: 'nowrap',
            }}
          >
            {label}
          </div>
        )}
      </div>
    );
  },
);

BoundingBox.displayName = 'BoundingBox';

interface DocumentPage {
  Id: string;
  ImageUri?: string;
}

interface BoundingBoxOverlay {
  geometry: BoundingBoxGeometry;
  color?: string;
  label?: string;
}

export interface PageImageViewerHandle {
  zoomToField: (geometry: BoundingBoxGeometry) => void;
}

interface PageImageViewerProps {
  pageIds?: string[];
  documentPages?: DocumentPage[];
  activeFieldGeometry?: BoundingBoxGeometry | null;
  onPageChange?: ((pageId: string) => void) | null;
  initialPage?: string | null;
  height?: string;
  showControls?: boolean;
  boundingBoxes?: BoundingBoxOverlay[];
  /**
   * Optional handle exposing `zoomToField`, so a caller can bind zooming to its
   * own gesture (human review uses double-click) instead of to every highlight.
   */
  zoomHandle?: React.Ref<PageImageViewerHandle>;
}

const PageImageViewer = ({
  pageIds = [],
  documentPages = [],
  activeFieldGeometry = null,
  onPageChange = null,
  initialPage = null,
  height = '700px',
  showControls = true,
  boundingBoxes = [],
  zoomHandle,
}: PageImageViewerProps): React.JSX.Element => {
  const { currentCredentials } = useAppContext();
  // Pin page images to the selected run's object versions when viewing history.
  const { versionIdForUri, runId: viewingRunId } = useDocumentVersion();
  const [pageImages, setPageImages] = useState<Record<string, string>>({});
  const [loadingImages, setLoadingImages] = useState(true);
  const [currentPage, setCurrentPage] = useState<string | null>(initialPage || (pageIds.length > 0 ? pageIds[0] : null));
  const [zoomLevel, setZoomLevel] = useState(1);
  const [panOffset, setPanOffset] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
  const imageRef = useRef<HTMLImageElement | null>(null);
  const imageContainerRef = useRef<HTMLDivElement | null>(null);

  // Load page images
  useEffect(() => {
    const loadImages = async () => {
      if (!pageIds || pageIds.length === 0) {
        setLoadingImages(false);
        return;
      }

      setLoadingImages(true);

      try {
        logger.debug('PageImageViewer - Loading images for pageIds:', pageIds);
        const images: Record<string, string> = {};

        await Promise.all(
          pageIds.map(async (pageId) => {
            const page = documentPages.find((p) => p.Id === pageId);

            if (page?.ImageUri) {
              // Only s3:// URIs need presigning; blob:/data:/https: URIs
              // (e.g. pages rendered client-side from a PDF) are usable as-is.
              if (!page.ImageUri.startsWith('s3://')) {
                images[pageId] = page.ImageUri;
                return;
              }
              try {
                logger.debug(`PageImageViewer - generating presigned URL for page ${pageId}`);
                const url = await generateS3PresignedUrl(page.ImageUri, currentCredentials as Record<string, unknown>, {
                  versionId: versionIdForUri(page.ImageUri),
                });
                images[pageId] = url;
              } catch (err) {
                logger.error(`Error generating presigned URL for page ${pageId}:`, err);
              }
            }
          }),
        );

        logger.debug('PageImageViewer - Successfully loaded images for', Object.keys(images).length, 'pages');
        setPageImages(images);

        // Also reset when the page list changed and no longer contains the
        // current page (e.g. the ground-truth editor switching to a section
        // whose split_document.page_indices are a different page set) —
        // otherwise the viewer waits forever on an image that will never load.
        if (pageIds.length > 0 && (!currentPage || !pageIds.includes(currentPage))) {
          setCurrentPage(pageIds[0]);
        }
      } catch (err) {
        logger.error('Error loading page images:', err);
      } finally {
        setLoadingImages(false);
      }
    };

    loadImages();
  }, [pageIds, documentPages, currentCredentials, viewingRunId]);

  // Handle geometry-based page switching
  useEffect(() => {
    if (activeFieldGeometry && activeFieldGeometry.page !== undefined && pageIds.length > 0) {
      const pageIndex = activeFieldGeometry.page - 1;
      if (pageIndex >= 0 && pageIndex < pageIds.length) {
        const targetPageId = pageIds[pageIndex];
        if (targetPageId !== currentPage) {
          setCurrentPage(targetPageId);
          if (onPageChange) {
            onPageChange(targetPageId);
          }
        }
      }
    }
  }, [activeFieldGeometry, pageIds, currentPage, onPageChange]);

  // Zoom controls
  const handleZoomIn = useCallback(() => {
    setZoomLevel((prev) => Math.min(prev * 1.25, 4));
  }, []);

  const handleZoomOut = useCallback(() => {
    setZoomLevel((prev) => Math.max(prev / 1.25, 0.25));
  }, []);

  // Pan controls
  const panStep = 50;
  const handlePanLeft = useCallback(() => setPanOffset((prev) => ({ ...prev, x: prev.x + panStep })), []);
  const handlePanRight = useCallback(() => setPanOffset((prev) => ({ ...prev, x: prev.x - panStep })), []);
  const handlePanUp = useCallback(() => setPanOffset((prev) => ({ ...prev, y: prev.y + panStep })), []);
  const handlePanDown = useCallback(() => setPanOffset((prev) => ({ ...prev, y: prev.y - panStep })), []);
  const handleResetView = useCallback(() => {
    setZoomLevel(1);
    setPanOffset({ x: 0, y: 0 });
  }, []);

  // Handle mouse wheel for zoom (matches the section Visual Editor: plain wheel zooms)
  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    const delta = e.deltaY < 0 ? 1.1 : 0.9;
    setZoomLevel((prev) => Math.min(Math.max(prev * delta, 0.25), 4));
  }, []);

  // Drag-to-pan (only meaningful when zoomed in), mirroring the section editor.
  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (zoomLevel <= 1) return;
      e.preventDefault();
      setIsDragging(true);
      setDragStart({ x: e.clientX - panOffset.x, y: e.clientY - panOffset.y });
    },
    [zoomLevel, panOffset],
  );

  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (!isDragging) return;
      e.preventDefault();
      setPanOffset({ x: e.clientX - dragStart.x, y: e.clientY - dragStart.y });
    },
    [isDragging, dragStart],
  );

  const handleMouseUp = useCallback(() => setIsDragging(false), []);
  const handleMouseLeave = useCallback(() => setIsDragging(false), []);

  // Handle page navigation
  const goToPreviousPage = useCallback(() => {
    if (currentPage === null) return;
    const currentIndex = pageIds.indexOf(currentPage);
    if (currentIndex > 0) {
      const newPage = pageIds[currentIndex - 1];
      setCurrentPage(newPage);
      if (onPageChange) {
        onPageChange(newPage);
      }
    }
  }, [currentPage, pageIds, onPageChange]);

  const goToNextPage = useCallback(() => {
    if (currentPage === null) return;
    const currentIndex = pageIds.indexOf(currentPage);
    if (currentIndex < pageIds.length - 1) {
      const newPage = pageIds[currentIndex + 1];
      setCurrentPage(newPage);
      if (onPageChange) {
        onPageChange(newPage);
      }
    }
  }, [currentPage, pageIds, onPageChange]);

  /**
   * Zoom to a field and centre it.
   *
   * Measured from the element's LAYOUT box (offsetWidth/offsetLeft), not
   * getBoundingClientRect(): the rect reports the already-transformed box, so
   * feeding it back in folds the current zoom and pan into the next calculation
   * and the image walks off toward a corner on each call.
   *
   * The image is scaled about its centre, so a point sitting `d` px from that
   * centre unzoomed sits `d * zoom` px from it once scaled — hence the single
   * multiplication by targetZoom below. `panOffset` is in screen px (the render
   * divides it back out by the zoom inside translate()).
   */
  const zoomToField = useCallback((geometry: BoundingBoxGeometry) => {
    const bbox = geometry?.boundingBox;
    if (!bbox || !imageRef.current || !imageContainerRef.current) return;

    const targetZoom = 2.0;
    setZoomLevel(targetZoom);

    // Deferred one frame: the layout box is stable, but the browser has not yet
    // applied the new zoom, and reading mid-transition yields a torn value.
    requestAnimationFrame(() => {
      const img = imageRef.current;
      const container = imageContainerRef.current;
      if (!img || !container) return;

      const w = img.offsetWidth;
      const h = img.offsetHeight;
      if (!w || !h) return;

      const centreX = img.offsetLeft + w / 2;
      const centreY = img.offsetTop + h / 2;
      const fieldU = bbox.left + bbox.width / 2;
      const fieldV = bbox.top + bbox.height / 2;

      setPanOffset({
        x: container.clientWidth / 2 - centreX - (fieldU - 0.5) * w * targetZoom,
        y: container.clientHeight / 2 - centreY - (fieldV - 0.5) * h * targetZoom,
      });
    });
  }, []);

  // Exposed imperatively rather than fired from an effect on activeFieldGeometry:
  // highlighting a field and zooming to it are separate gestures (single- vs
  // double-click), and an effect keyed on the geometry cannot tell them apart.
  useImperativeHandle(zoomHandle, () => ({ zoomToField }), [zoomToField]);

  const fallbackImage =
    'data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgeG1sbnM9Imh0dHA6' +
    'Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgZmlsbD0iI2Yw' +
    'ZjBmMCIvPjx0ZXh0IHg9IjUwJSIgeT0iNTAlIiBmb250LWZhbWlseT0iQXJpYWwiIGZvbnQtc2l6ZT0iMjAi' +
    'IHRleHQtYW5jaG9yPSJtaWRkbGUiIGZpbGw9IiM5OTkiPkltYWdlIGxvYWQgZXJyb3I8L3RleHQ+PC9zdmc+';

  if (loadingImages) {
    return (
      <div style={{ height }}>
        <Box padding="xl" textAlign="center">
          <Spinner />
          <div>Loading page images...</div>
        </Box>
      </div>
    );
  }

  if (pageIds.length === 0) {
    return (
      <div style={{ height }}>
        <Box padding="xl" textAlign="center">
          No page images available
        </Box>
      </div>
    );
  }

  const currentPageIndex = currentPage !== null ? pageIds.indexOf(currentPage) : -1;

  return (
    <div style={{ position: 'relative', height, overflow: 'hidden' }}>
      {/* Image container - must exactly match VisualEditorModal structure for bounding box positioning
          The BoundingBox calculates position relative to this container */}
      {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
      <div
        ref={imageContainerRef}
        style={{
          position: 'relative',
          width: '100%',
          height: showControls ? 'calc(100% - 90px)' : '100%',
          display: 'flex',
          justifyContent: 'center',
          overflow: 'hidden',
          cursor: isDragging ? 'grabbing' : zoomLevel > 1 ? 'grab' : 'default',
          userSelect: 'none',
        }}
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseLeave}
      >
        {currentPage !== null && pageImages[currentPage] ? (
          <>
            <img
              ref={imageRef}
              src={pageImages[currentPage]}
              alt={`Page ${currentPage}`}
              style={{
                maxWidth: '100%',
                maxHeight: '100%',
                width: 'auto',
                height: 'auto',
                objectFit: 'contain',
                transform: `scale(${zoomLevel}) translate(${panOffset.x / zoomLevel}px, ${panOffset.y / zoomLevel}px)`,
                transformOrigin: 'center center',
                transition: 'transform 0.1s ease-out',
              }}
              onError={(e) => {
                logger.error(`Error loading image for page ${currentPage}:`, e);
                (e.target as HTMLImageElement).src = fallbackImage;
              }}
            />
            {/* Active field bounding box - matching the VisualEditorModal pattern exactly:
                both page and currentPage use the same currentPage value (pageId) */}
            {activeFieldGeometry && (
              <BoundingBox
                box={activeFieldGeometry}
                page={currentPage}
                currentPage={currentPage}
                imageRef={imageRef}
                containerRef={imageContainerRef}
                zoomLevel={zoomLevel}
                panOffset={panOffset}
              />
            )}
            {/* Additional bounding boxes */}
            {boundingBoxes.map((bbox) => (
              <BoundingBox
                key={`bbox-${bbox.label || ''}-${bbox.geometry?.boundingBox?.left || 0}-${bbox.geometry?.boundingBox?.top || 0}`}
                box={bbox.geometry}
                page={currentPage}
                currentPage={currentPage}
                imageRef={imageRef}
                containerRef={imageContainerRef}
                zoomLevel={zoomLevel}
                panOffset={panOffset}
                color={bbox.color || 'blue'}
                label={bbox.label}
              />
            ))}
          </>
        ) : (
          <Box padding="xl" textAlign="center">
            <Spinner />
            <div>Loading image...</div>
          </Box>
        )}
      </div>

      {/* Navigation buttons */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          position: 'absolute',
          width: '100%',
          top: '50%',
          transform: 'translateY(-50%)',
          pointerEvents: 'none',
          padding: '0 8px',
          boxSizing: 'border-box',
        }}
      >
        <div style={{ pointerEvents: 'auto' }}>
          <Button iconName="angle-left" variant="icon" onClick={goToPreviousPage} disabled={currentPageIndex === 0} />
        </div>
        <div style={{ pointerEvents: 'auto' }}>
          <Button iconName="angle-right" variant="icon" onClick={goToNextPage} disabled={currentPageIndex === pageIds.length - 1} />
        </div>
      </div>

      {/* Controls */}
      {showControls && (
        <div
          style={{
            position: 'absolute',
            bottom: '10px',
            width: '100%',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: '8px',
          }}
        >
          {/* Page indicator */}
          <div
            style={{
              backgroundColor: 'rgba(255, 255, 255, 0.8)',
              padding: '4px 8px',
              borderRadius: '4px',
            }}
          >
            Page {currentPageIndex + 1} of {pageIds.length}
          </div>

          {/* Zoom and Pan Controls */}
          <div
            style={{
              backgroundColor: 'rgba(255, 255, 255, 0.9)',
              padding: '4px 8px',
              borderRadius: '4px',
              boxShadow: '0 1px 4px rgba(0, 0, 0, 0.1)',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              fontSize: '12px',
            }}
          >
            <span style={{ fontWeight: 'bold' }}>Zoom:</span>
            <span
              onClick={handleZoomOut}
              onKeyDown={(e) => e.key === 'Enter' && handleZoomOut()}
              role="button"
              tabIndex={0}
              style={{
                cursor: zoomLevel <= 0.25 ? 'not-allowed' : 'pointer',
                opacity: zoomLevel <= 0.25 ? 0.5 : 1,
                fontSize: '14px',
                fontWeight: 'bold',
                userSelect: 'none',
                padding: '2px 4px',
              }}
              title="Zoom Out"
            >
              −
            </span>
            <span style={{ fontSize: '12px', minWidth: '30px', textAlign: 'center' }}>{Math.round(zoomLevel * 100)}%</span>
            <span
              onClick={handleZoomIn}
              onKeyDown={(e) => e.key === 'Enter' && handleZoomIn()}
              role="button"
              tabIndex={0}
              style={{
                cursor: zoomLevel >= 4 ? 'not-allowed' : 'pointer',
                opacity: zoomLevel >= 4 ? 0.5 : 1,
                fontSize: '14px',
                fontWeight: 'bold',
                userSelect: 'none',
                padding: '2px 4px',
              }}
              title="Zoom In"
            >
              +
            </span>
            <span style={{ fontWeight: 'bold', marginLeft: '4px' }}>Pan:</span>
            <span
              onClick={handlePanLeft}
              onKeyDown={(e) => e.key === 'Enter' && handlePanLeft()}
              role="button"
              tabIndex={0}
              style={{
                cursor: zoomLevel <= 1 ? 'not-allowed' : 'pointer',
                opacity: zoomLevel <= 1 ? 0.5 : 1,
                fontSize: '14px',
                userSelect: 'none',
                padding: '2px 3px',
              }}
              title="Pan Left"
            >
              ←
            </span>
            <span
              onClick={handlePanRight}
              onKeyDown={(e) => e.key === 'Enter' && handlePanRight()}
              role="button"
              tabIndex={0}
              style={{
                cursor: zoomLevel <= 1 ? 'not-allowed' : 'pointer',
                opacity: zoomLevel <= 1 ? 0.5 : 1,
                fontSize: '14px',
                userSelect: 'none',
                padding: '2px 3px',
              }}
              title="Pan Right"
            >
              →
            </span>
            <span
              onClick={handlePanUp}
              onKeyDown={(e) => e.key === 'Enter' && handlePanUp()}
              role="button"
              tabIndex={0}
              style={{
                cursor: zoomLevel <= 1 ? 'not-allowed' : 'pointer',
                opacity: zoomLevel <= 1 ? 0.5 : 1,
                fontSize: '14px',
                userSelect: 'none',
                padding: '2px 3px',
              }}
              title="Pan Up"
            >
              ↑
            </span>
            <span
              onClick={handlePanDown}
              onKeyDown={(e) => e.key === 'Enter' && handlePanDown()}
              role="button"
              tabIndex={0}
              style={{
                cursor: zoomLevel <= 1 ? 'not-allowed' : 'pointer',
                opacity: zoomLevel <= 1 ? 0.5 : 1,
                fontSize: '14px',
                userSelect: 'none',
                padding: '2px 3px',
              }}
              title="Pan Down"
            >
              ↓
            </span>
            <span
              onClick={handleResetView}
              onKeyDown={(e) => e.key === 'Enter' && handleResetView()}
              role="button"
              tabIndex={0}
              style={{
                cursor: 'pointer',
                fontSize: '12px',
                userSelect: 'none',
                padding: '2px 3px',
                marginLeft: '2px',
              }}
              title="Reset View"
            >
              ⟲
            </span>
          </div>
        </div>
      )}
    </div>
  );
};

export default PageImageViewer;
