// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// My Work › Favorites: starred queues and documents, kept in this browser per
// signed-in user. Every mounted consumer stays in step (same tab and other tabs).
import { useCallback, useEffect, useMemo, useState } from 'react';

import useAppContext from '../contexts/app';

export type FavoriteKind = 'review-queue' | 'test-set' | 'review-item' | 'test-set-item';

export interface Favorite {
  /** Unique per favorite, e.g. "test-set:fake-w2". */
  id: string;
  kind: FavoriteKind;
  name: string;
  /** Hash link to open it. */
  href: string;
  /** Extra context shown beside the name (profile, test set). */
  context?: string;
  addedAt: string;
}

const KEY_PREFIX = 'idp-my-work-favorites:';
const CHANGE_EVENT = 'idp-favorites-changed';

const read = (key: string): Favorite[] => {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as Favorite[]) : [];
  } catch {
    return [];
  }
};

interface UseFavoritesReturn {
  favorites: readonly Favorite[];
  isFavorite: (id: string) => boolean;
  toggle: (favorite: Omit<Favorite, 'addedAt'>) => void;
  remove: (id: string) => void;
}

const useFavorites = (): UseFavoritesReturn => {
  const { user } = useAppContext();
  const key = `${KEY_PREFIX}${user?.userId ?? user?.username ?? 'anonymous'}`;
  const [favorites, setFavorites] = useState<Favorite[]>(() => read(key));

  useEffect(() => {
    setFavorites(read(key));
    const sync = () => setFavorites(read(key));
    window.addEventListener(CHANGE_EVENT, sync);
    window.addEventListener('storage', sync);
    return () => {
      window.removeEventListener(CHANGE_EVENT, sync);
      window.removeEventListener('storage', sync);
    };
  }, [key]);

  const write = useCallback(
    (next: Favorite[]) => {
      try {
        localStorage.setItem(key, JSON.stringify(next));
      } catch {
        // Storage full or disabled: the star simply doesn't persist.
      }
      window.dispatchEvent(new Event(CHANGE_EVENT));
    },
    [key],
  );

  const ids = useMemo(() => new Set(favorites.map((f) => f.id)), [favorites]);
  const isFavorite = useCallback((id: string) => ids.has(id), [ids]);
  const remove = useCallback((id: string) => write(read(key).filter((f) => f.id !== id)), [key, write]);
  const toggle = useCallback(
    (favorite: Omit<Favorite, 'addedAt'>) => {
      const current = read(key);
      write(
        current.some((f) => f.id === favorite.id)
          ? current.filter((f) => f.id !== favorite.id)
          : [...current, { ...favorite, addedAt: new Date().toISOString() }],
      );
    },
    [key, write],
  );

  return { favorites, isFavorite, toggle, remove };
};

export default useFavorites;
