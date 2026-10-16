// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { Button } from '@cloudscape-design/components';

import useFavorites from '../../hooks/use-favorites';
import type { Favorite } from '../../hooks/use-favorites';

/** A star that adds or removes something from My Work › Favorites. */
const FavoriteButton = ({ favorite }: { favorite: Omit<Favorite, 'addedAt'> }): React.JSX.Element => {
  const { isFavorite, toggle } = useFavorites();
  const on = isFavorite(favorite.id);
  return (
    <Button
      variant="icon"
      iconName={on ? 'star-filled' : 'star'}
      ariaLabel={on ? `Remove ${favorite.name} from favorites` : `Add ${favorite.name} to favorites`}
      onClick={() => toggle(favorite)}
    />
  );
};

export default FavoriteButton;
