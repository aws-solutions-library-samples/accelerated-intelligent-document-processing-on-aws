// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The two server operations the Routing tab uses besides saving the configuration:
// testing a table against a processed document, and converting it to and from DMN XML.
import { generateClient } from '../../api/client-shim';
import { convertRoutingDmn as convertRoutingDmnQuery, testRoutingTable as testRoutingTableQuery } from '../../graphql/generated';
import type { RoutingTestResult } from '../../graphql/generated/operation-types';
import { readRouting, type RoutingConfig } from './routing-types';

const client = generateClient();

export type DmnVersion = '1.5' | '1.3';

/**
 * What a routing table decides for a processed document. `routing` is the table being
 * edited, saved or not; without it the server tests the configuration the document is
 * pinned to.
 */
export const testRoutingTable = async (objectKey: string, routing?: RoutingConfig): Promise<RoutingTestResult> => {
  const r = await client.graphql({
    query: testRoutingTableQuery,
    variables: { objectKey, routing: routing ? JSON.stringify(routing) : undefined },
  });
  return r.data.testRoutingTable;
};

/** The inputs a test read, as name and value pairs. The server sends them as JSON text. */
export const testInputs = (result: Pick<RoutingTestResult, 'inputs'>): [string, unknown][] => {
  if (!result.inputs) return [];
  try {
    const parsed: unknown = typeof result.inputs === 'string' ? JSON.parse(result.inputs) : result.inputs;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? Object.entries(parsed) : [];
  } catch {
    return [];
  }
};

/** The routing section as DMN XML. */
export const routingToDmn = async (routing: RoutingConfig, dmnVersion: DmnVersion): Promise<string> => {
  const r = await client.graphql({
    query: convertRoutingDmnQuery,
    variables: { direction: 'to_dmn', content: JSON.stringify(routing), dmnVersion },
  });
  return r.data.convertRoutingDmn.xml ?? '';
};

/** A DMN file's decision table as a routing section. The server refuses what it cannot read. */
export const routingFromDmn = async (xml: string): Promise<RoutingConfig> => {
  const r = await client.graphql({ query: convertRoutingDmnQuery, variables: { direction: 'from_dmn', content: xml } });
  const routing = r.data.convertRoutingDmn.routing;
  return readRouting(typeof routing === 'string' ? JSON.parse(routing) : routing);
};
