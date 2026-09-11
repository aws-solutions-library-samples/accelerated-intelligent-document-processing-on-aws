// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect } from 'react';
import { fetchSharedAuthSession } from '../api/auth-session';
import { ConsoleLogger } from 'aws-amplify/utils';

const logger = new ConsoleLogger('useUserDisplayName');

interface UserDisplayNameReturn {
  displayName: string | null;
  loading: boolean;
}

const useUserDisplayName = (): UserDisplayNameReturn => {
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchDisplayName = async () => {
      try {
        const session = await fetchSharedAuthSession();
        const payload = session?.tokens?.idToken?.payload;
        const givenName = payload?.['given_name'] as string | undefined;
        const familyName = payload?.['family_name'] as string | undefined;
        const email = payload?.['email'] as string | undefined;

        if (givenName || familyName) {
          setDisplayName([givenName, familyName].filter(Boolean).join(' '));
        } else if (email) {
          setDisplayName(email);
        }
      } catch (error) {
        logger.error('Error fetching user display name:', error);
      } finally {
        setLoading(false);
      }
    };
    fetchDisplayName();
  }, []);

  return { displayName, loading };
};

export default useUserDisplayName;
