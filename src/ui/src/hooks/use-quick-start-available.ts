// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import useUserRole from './use-user-role';
import { isQuickStartEnabled } from '../utils/quick-start-flag';

/**
 * Whether this user is offered Quick Start: the stack leaves it on and the user is an
 * Admin. Quick Start creates and activates a configuration profile, which only an
 * Admin may create, and the chat routes refuse a Quick Start turn from anyone else.
 */
const useQuickStartAvailable = (): boolean => {
  const { isAdmin } = useUserRole();
  return isQuickStartEnabled() && isAdmin;
};

export default useQuickStartAvailable;
