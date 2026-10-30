// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect, useLayoutEffect, useRef, memo, useCallback, useImperativeHandle } from 'react';
import { Box, Spinner, Button } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { presignBatch, presignedUrlsNeedRefresh, type PresignBatchResult } from '../../api/presign-batch';
import { FILE_ACCESS_DENIED_MESSAGE } from '../../hooks/utils/graphql-error';
import { rotateBoundingBox, rotateOffset, rotationFitScale } from './page-rotation-utils';
import type { NormalisedBox } from './page-rotation-utils';
import { useDocumentVersion } from '../../contexts/document-version';

const logger = new ConsoleLogger('PageImageViewer');

interface BoundingBoxGeometry {
  boundingBox?: NormalisedBox;
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
  /** Clockwise page rotation in degrees; see {@link rotateBoundingBox}. */
  rotation?: number;
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
    rotation = 0,
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
      // `rotation` belongs here for the same reason zoom and pan do: it changes
      // the rect this reads, including swapping its width and height.
    }, [zoomLevel, panOffset, rotation, imageRef, containerRef, page, currentPage]);

    if (page !== currentPage || !box || !dimensions.transformedWidth) {
      return null;
    }

    if (!box.boundingBox) {
      return null;
    }

    const padding = 5;
    const bbox = rotateBoundingBox(box.boundingBox, rotation);

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
  // Pin page images to the selected run's object versions when viewing history.
  const { versionIdForUri, runId: viewingRunId } = useDocumentVersion();
  const [pageImages, setPageImages] = useState<Record<string, string>>({});
  const [loadingImages, setLoadingImages] = useState(true);
  /**
   * Why a page has no URL, when that is the server's answer rather than the page
   * simply having no image. Without it a refused document showed a spinner for
   * ever: the resolver's per-document scope check fails the WHOLE batch with a
   * 403, so every page is missing at once and "still loading" is indistinguishable
   * from "never allowed" (issue #1033).
   */
  const [imageErrorMessage, setImageErrorMessage] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState<string | null>(initialPage || (pageIds.length > 0 ? pageIds[0] : null));
  const [zoomLevel, setZoomLevel] = useState(1);
  const [rotation, setRotation] = useState(0);
  const [rotationFit, setRotationFit] = useState(1);
  const [panOffset, setPanOffset] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
  const imageRef = useRef<HTMLImageElement | null>(null);
  const imageContainerRef = useRef<HTMLDivElement | null>(null);

  /**
   * Re-signing, and why it is driven from page navigation rather than from a clock.
   *
   * The URLs in `pageImages` are bearer capabilities minted by the resolver with a
   * 300 s life (`_PRESIGNED_URL_EXPIRY_SECONDS`, see `api/presign-batch` for the
   * whole trade). The signing effect below depends on `[pageIds, documentPages,
   * viewingRunId]`, and for a **completed** document none of those ever changes
   * again — detail polling has stopped — so nothing re-fires it. Open such a
   * document, leave it five minutes and page forward, and the new page's `<img>`
   * fetches an expired URL: S3 answers 403 and the viewer shows its fallback, which
   * reads as flakiness rather than as an expiry.
   *
   * An expired URL only matters at the moment a **new** image is fetched — bytes
   * already displayed are not re-fetched — so checking staleness exactly there
   * covers every case where one would be used, at no cost on an idle tab. `signedRef`
   * holds the last signing's outcome so the check needs no state, and bumping
   * `refreshToken` is what re-fires the effect.
   */
  const [refreshToken, setRefreshToken] = useState(0);
  const signedRef = useRef<PresignBatchResult | null>(null);

  /**
   * Called just before a page the user has not been shown yet becomes current.
   * Not from render and not from an effect keyed on `currentPage`: a render-time
   * side effect risks a signing loop, and the two navigation paths are the only
   * places a new image is needed.
   */
  const refreshSignedUrlsIfStale = useCallback(() => {
    if (!presignedUrlsNeedRefresh(signedRef.current)) return;
    // Cleared before bumping so that two quick navigations while the re-sign is
    // still in flight raise the token once, not once each.
    signedRef.current = null;
    setRefreshToken((prev) => prev + 1);
  }, []);

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

        // One resolver call for every s3:// page rather than one per page — see
        // `api/presign-batch` for why the browser no longer signs these itself.
        const toSign: Array<{ pageId: string; s3Uri: string }> = [];
        for (const pageId of pageIds) {
          const uri = documentPages.find((p) => p.Id === pageId)?.ImageUri;
          if (!uri) continue;
          // Only s3:// URIs need presigning; blob:/data:/https: URIs
          // (e.g. pages rendered client-side from a PDF) are usable as-is.
          if (!uri.startsWith('s3://')) {
            images[pageId] = uri;
            continue;
          }
          toSign.push({ pageId, s3Uri: uri });
        }

        let failureMessage: string | null = null;
        // Nothing signed yet for this run of the effect. A view whose pages are all
        // `blob:`/`data:`/`https:` leaves it null and so never re-signs — there is
        // no capability to expire.
        signedRef.current = null;
        if (toSign.length > 0) {
          logger.debug(`PageImageViewer - presigning ${toSign.length} page image(s) in one batch`);
          const signed = await presignBatch(toSign.map(({ s3Uri }) => ({ s3Uri, versionId: versionIdForUri(s3Uri) })));
          // Kept so a later navigation can ask how old these URLs are.
          signedRef.current = signed;
          let missing = 0;
          for (const { pageId, s3Uri } of toSign) {
            const url = signed.get(s3Uri, versionIdForUri(s3Uri));
            if (url) images[pageId] = url;
            else {
              missing += 1;
              logger.error(`No presigned URL returned for page ${pageId}`);
            }
          }
          // The reason comes from the call, not from counting absences: the whole
          // batch is refused together, so a 403 and "this document has no page
          // images" produce the same empty record.
          if (signed.accessDenied) failureMessage = FILE_ACCESS_DENIED_MESSAGE;
          else if (signed.failed || missing > 0) failureMessage = 'The page images for this document could not be loaded.';
        }
        setImageErrorMessage(failureMessage);

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
    // `refreshToken` is the re-sign trigger; see `refreshSignedUrlsIfStale`. The
    // effect's `setLoadingImages(true)` therefore shows the spinner for the length
    // of a re-sign, which is the honest surface: the page being navigated to has no
    // usable URL until the new batch arrives.
  }, [pageIds, documentPages, viewingRunId, refreshToken]);

  // Handle geometry-based page switching
  useEffect(() => {
    if (activeFieldGeometry && activeFieldGeometry.page !== undefined && pageIds.length > 0) {
      const pageIndex = activeFieldGeometry.page - 1;
      if (pageIndex >= 0 && pageIndex < pageIds.length) {
        const targetPageId = pageIds[pageIndex];
        if (targetPageId !== currentPage) {
          // A programmatic page move needs a new image just as much as the
          // control does, so it is a re-sign trigger too.
          refreshSignedUrlsIfStale();
          setCurrentPage(targetPageId);
          if (onPageChange) {
            onPageChange(targetPageId);
          }
        }
      }
    }
  }, [activeFieldGeometry, pageIds, currentPage, onPageChange, refreshSignedUrlsIfStale]);

  // Zoom controls
  const handleZoomIn = useCallback(() => {
    setZoomLevel((prev) => Math.min(prev * 1.25, 4));
  }, []);

  const handleZoomOut = useCallback(() => {
    setZoomLevel((prev) => Math.max(prev / 1.25, 0.25));
  }, []);

  // Rotation controls. Kept in 0–359 rather than going negative so the readout
  // never reads "-90°", and sticky across page navigation like zoom and pan —
  // a scanned document is usually misoriented the same way on every page.
  const handleRotateLeft = useCallback(() => setRotation((prev) => (prev + 270) % 360), []);
  const handleRotateRight = useCallback(() => setRotation((prev) => (prev + 90) % 360), []);

  /**
   * Re-fit the page after a quarter turn — see `rotationFitScale`, which holds
   * the reasoning and the arithmetic.
   *
   * Reads the image's *layout* box, not getBoundingClientRect(): the rect is
   * already transformed, so feeding it back in would compound the scale on
   * every measurement.
   */
  const measureRotationFit = useCallback(() => {
    const img = imageRef.current;
    const container = imageContainerRef.current;
    const fit = rotationFitScale({
      imageWidth: img?.offsetWidth ?? 0,
      imageHeight: img?.offsetHeight ?? 0,
      containerWidth: container?.clientWidth ?? 0,
      containerHeight: container?.clientHeight ?? 0,
      rotation,
    });
    // null means nothing was measurable yet; keep the last good value.
    if (fit !== null) setRotationFit(fit);
  }, [rotation]);

  // Re-fit on rotation and on page change (pages need not share a shape).
  // Before paint, not after: the measurement reads the *layout* box, which the
  // transform does not affect, so it is already correct here — and taking it
  // after paint would show one frame of the rotated page at its unfitted size,
  // clipped by the container, before it settled.
  useLayoutEffect(() => {
    measureRotationFit();
  }, [measureRotationFit, currentPage, pageImages]);
  // Container resizes are a separate, post-layout concern (the editor's columns
  // are fluid).

  useEffect(() => {
    const container = imageContainerRef.current;
    if (!container || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(() => measureRotationFit());
    observer.observe(container);
    return () => observer.disconnect();
  }, [measureRotationFit]);

  // The scale actually applied: the user's zoom times the rotation re-fit.
  const effectiveScale = zoomLevel * rotationFit;

  // Pan controls
  const panStep = 50;
  const handlePanLeft = useCallback(() => setPanOffset((prev) => ({ ...prev, x: prev.x + panStep })), []);
  const handlePanRight = useCallback(() => setPanOffset((prev) => ({ ...prev, x: prev.x - panStep })), []);
  const handlePanUp = useCallback(() => setPanOffset((prev) => ({ ...prev, y: prev.y + panStep })), []);
  const handlePanDown = useCallback(() => setPanOffset((prev) => ({ ...prev, y: prev.y - panStep })), []);
  const handleResetView = useCallback(() => {
    setZoomLevel(1);
    setPanOffset({ x: 0, y: 0 });
    setRotation(0);
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
      // The moment a new image is needed: re-sign first if the URLs are stale.
      refreshSignedUrlsIfStale();
      setCurrentPage(newPage);
      if (onPageChange) {
        onPageChange(newPage);
      }
    }
  }, [currentPage, pageIds, onPageChange, refreshSignedUrlsIfStale]);

  const goToNextPage = useCallback(() => {
    if (currentPage === null) return;
    const currentIndex = pageIds.indexOf(currentPage);
    if (currentIndex < pageIds.length - 1) {
      const newPage = pageIds[currentIndex + 1];
      // The moment a new image is needed: re-sign first if the URLs are stale.
      refreshSignedUrlsIfStale();
      setCurrentPage(newPage);
      if (onPageChange) {
        onPageChange(newPage);
      }
    }
  }, [currentPage, pageIds, onPageChange, refreshSignedUrlsIfStale]);

  /**
   * Zoom to a field and centre it.
   *
   * Measured from the element's LAYOUT box (offsetWidth/offsetLeft), not
   * getBoundingClientRect(): the rect reports the already-transformed box, so
   * feeding it back in folds the current zoom and pan into the next calculation
   * and the image walks off toward a corner on each call.
   *
   * The image is scaled about its centre, so a point sitting `d` px from that
   * centre unzoomed sits `d * scale` px from it once scaled — hence the single
   * multiplication by the target scale below. `panOffset` is in screen px (the
   * render divides it back out by the scale inside translate()).
   *
   * Rotation is applied to `d` before scaling, mirroring the transform's own
   * order: the field's offset from the page centre turns with the page, so
   * without this the view centres on wherever that offset happens to point on
   * an unrotated page.
   */
  const zoomToField = useCallback(
    (geometry: BoundingBoxGeometry) => {
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

        // Offset of the field from the page centre, in unscaled layout px,
        // turned clockwise by the current rotation.
        const offset = rotateOffset((fieldU - 0.5) * w, (fieldV - 0.5) * h, rotation);
        const targetScale = targetZoom * rotationFit;

        setPanOffset({
          x: container.clientWidth / 2 - centreX - offset.x * targetScale,
          y: container.clientHeight / 2 - centreY - offset.y * targetScale,
        });
      });
    },
    [rotation, rotationFit],
  );

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
      {/* Image container: the BoundingBox calculates its position relative to this container */}
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
                // Read right to left: rotate about the centre, then translate,
                // then scale. Rotating last (i.e. innermost) is what keeps pan
                // aligned to the screen rather than to the page — otherwise
                // "pan left" moves a 90°-rotated page upwards. Dividing the
                // offset by the scale leaves pan in screen pixels, since the
                // outer scale multiplies it straight back.
                transform:
                  `scale(${effectiveScale}) ` +
                  `translate(${panOffset.x / effectiveScale}px, ${panOffset.y / effectiveScale}px) ` +
                  `rotate(${rotation}deg)`,
                transformOrigin: 'center center',
                transition: 'transform 0.1s ease-out',
              }}
              onLoad={measureRotationFit}
              onError={(e) => {
                logger.error(`Error loading image for page ${currentPage}:`, e);
                (e.target as HTMLImageElement).src = fallbackImage;
              }}
            />
            {/* Active field bounding box: page and currentPage use the same currentPage value (pageId) */}
            {activeFieldGeometry && (
              <BoundingBox
                box={activeFieldGeometry}
                page={currentPage}
                currentPage={currentPage}
                imageRef={imageRef}
                containerRef={imageContainerRef}
                zoomLevel={zoomLevel}
                panOffset={panOffset}
                rotation={rotation}
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
                rotation={rotation}
                color={bbox.color || 'blue'}
                label={bbox.label}
              />
            ))}
          </>
        ) : (
          /* Never a spinner here. Signing has finished by this point, so a page
             with no URL is a settled outcome — a refusal, a failure, or a page
             that carries no image — and each of those says so. */
          <Box padding="xl" textAlign="center" color={imageErrorMessage ? 'text-status-error' : 'inherit'}>
            {imageErrorMessage ?? 'No image is available for this page.'}
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
          <Button
            iconName="angle-left"
            variant="icon"
            ariaLabel="Previous page"
            onClick={goToPreviousPage}
            disabled={currentPageIndex === 0}
          />
        </div>
        <div style={{ pointerEvents: 'auto' }}>
          <Button
            iconName="angle-right"
            variant="icon"
            ariaLabel="Next page"
            onClick={goToNextPage}
            disabled={currentPageIndex === pageIds.length - 1}
          />
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
              // Wraps rather than overflowing: the row is centred and absolutely
              // positioned, so overflow would clip both ends and take the reset
              // control with it.
              flexWrap: 'wrap',
              justifyContent: 'center',
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
            <span style={{ fontWeight: 'bold', marginLeft: '4px' }}>Rotate:</span>
            <span
              onClick={handleRotateLeft}
              onKeyDown={(e) => e.key === 'Enter' && handleRotateLeft()}
              role="button"
              tabIndex={0}
              style={{
                cursor: 'pointer',
                fontSize: '14px',
                userSelect: 'none',
                padding: '2px 3px',
              }}
              title="Rotate Left 90°"
              // The content is a glyph, so without this the accessible name is
              // "anticlockwise open circle arrow". `title` is only a fallback
              // and is not announced dependably.
              aria-label="Rotate left 90 degrees"
            >
              ↺
            </span>
            <span style={{ fontSize: '12px', minWidth: '30px', textAlign: 'center' }}>{rotation}°</span>
            <span
              onClick={handleRotateRight}
              onKeyDown={(e) => e.key === 'Enter' && handleRotateRight()}
              role="button"
              tabIndex={0}
              style={{
                cursor: 'pointer',
                fontSize: '14px',
                userSelect: 'none',
                padding: '2px 3px',
              }}
              title="Rotate Right 90°"
              aria-label="Rotate right 90 degrees"
            >
              ↻
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
