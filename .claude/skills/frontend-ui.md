# Frontend UI Development — GenAI IDP Accelerator

## Stack
- **Framework**: React 18.3 + TypeScript 5.9 (strict mode)
- **UI Library**: Cloudscape Design System v3 (`@cloudscape-design/components`)
- **Bundler**: Vite 7.3 with `@vitejs/plugin-react` (automatic JSX)
- **Auth**: AWS Amplify v6 + Cognito (`@aws-amplify/ui-react`)
- **API**: API Gateway REST API — `POST /op/<field>` behind a Cognito User Pools authorizer — reached through a GraphQL-shaped client shim (`src/api/client-shim.ts` → `src/api/rest-client.ts`). Operation documents and TypeScript types are still generated from `schema.graphql` via `codegen.config.mjs`. AppSync has been removed.
- **Router**: react-router-dom v6 (HashRouter)
- **State**: React Context + `immer` (NO Redux)
- **Node**: `>=22.12.0`, npm `>=10.0.0`
- **Module**: ESM (`"type": "module"`)
- **Test**: Vitest + jsdom

## Path Alias
`@/` → `./src/` (configured in both `tsconfig.json` and `vite.config.js`)
```tsx
import { useAppContext } from '@/contexts/app';
```

## Component Pattern (MUST follow)
Arrow function components are ENFORCED by ESLint:
```tsx
import React, { useState, useEffect, useMemo } from 'react';
import { Table, Pagination, TextFilter, Box, SpaceBetween } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { ConsoleLogger } from 'aws-amplify/utils';

const logger = new ConsoleLogger('MyComponent');

const MyComponent = (): React.JSX.Element => {
  // 1. Hooks (context, state, refs, navigation)
  const { user } = useAppContext();
  const [data, setData] = useState<MyType[]>([]);
  const navigate = useNavigate();

  // 2. Effects
  useEffect(() => {
    // ...
  }, []);

  // 3. Memos / derived state
  const filtered = useMemo(() => data.filter(...), [data]);

  // 4. Handlers
  const handleClick = () => { ... };

  // 5. Render
  return (
    <SpaceBetween size="l">
      <Table items={filtered} ... />
    </SpaceBetween>
  );
};

export default MyComponent;
```

## Directory Structure
```
src/ui/src/
├── App.tsx              # Root: ThemeProvider > Authenticator.Provider > AppContent
├── index.tsx            # Entry point
├── components/          # 27 feature directories + common/
│   ├── common/          # Shared (tables, modals, labels, download helpers)
│   ├── agent-chat/
│   ├── document-list/
│   ├── document-details/
│   ├── configuration-layout/
│   ├── json-schema-builder/
│   ├── test-studio/
│   └── ...
├── contexts/            # React Context providers
│   ├── app.ts           # AppContext (auth, config, navigation)
│   ├── agentChat.tsx
│   ├── analytics.tsx
│   ├── documents.ts
│   └── settings.ts
├── api/                 # Transport: rest-client.ts, client-shim.ts (GraphQL-shaped
│                        # facade), stream-client.ts (chat SSE), auth-session.ts
├── hooks/               # 35 custom hooks (use-kebab-case.ts for new ones)
├── routes/              # Route definitions (AuthRoutes, UnauthRoutes, etc.)
├── graphql/             # Operation documents + generated types — DO NOT EDIT
│                        # generated/ manually; run `make codegen`
├── types/               # TypeScript type definitions
├── utils/               # Utility functions
├── constants/           # App constants
└── data/                # Static data (e.g., standard-classes.json)
```

## Context Pattern
```tsx
// Definition (contexts/app.ts)
export interface AppContextValue {
  authState: string;
  awsConfig: Record<string, unknown> | undefined;
  user: AuthUser | undefined;
}
export const AppContext = createContext<AppContextValue | null>(null);
const useAppContext = (): AppContextValue => {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useAppContext must be used within AppContext.Provider');
  return ctx;
};
export default useAppContext;
```

## Auth Pattern
```tsx
import { useAuthenticator } from '@aws-amplify/ui-react';
import { fetchAuthSession } from 'aws-amplify/auth';

const { user, signOut } = useAuthenticator();
const session = await fetchAuthSession();
const credentials = session.credentials;
```

## Logging Pattern
Use Amplify's ConsoleLogger per component:
```tsx
import { ConsoleLogger } from 'aws-amplify/utils';
const logger = new ConsoleLogger('ComponentName');
logger.debug('message');
logger.error('error', error);
```

## API transport (GraphQL-shaped, REST underneath)

AWS AppSync has been removed; **zero `AWS::AppSync` resources exist in any
template**. The UI calls a single route — `POST /op/{field}` on an API Gateway
REST API (logical id `HttpApi` in `nested/api-resolvers/template.yaml`) behind
the `HttpApiAuthorizer` Cognito User Pools authorizer — which is served by the
dispatcher Lambda `HttpApiDispatcherFunction`. The dispatcher looks the field up
in a field→function map and either invokes the resolver Lambda or answers
in-process from DynamoDB. See `docs/migration-appsync-to-rest.md`.

- The call shape is carried over from the Amplify/AppSync client and deliberately
  left unchanged:
  `import { generateClient } from '@/api/client-shim'`, then
  `await client.graphql({ query, variables })`. The shim parses the field name
  out of the query document and POSTs to `${VITE_API_BASE_URL}/op/<field>`.
  Do **not** import `generateClient` from `aws-amplify/api` — no GraphQL
  endpoint is configured in Amplify, so it throws. Amplify is used for Cognito
  token retrieval only.
- Types and operation documents are auto-generated via `make codegen` (uses
  `codegen.config.mjs`) from `nested/api-resolvers/src/api/schema.graphql`.
  Generated files live in `src/graphql/generated/` — NEVER edit manually.
  `make codegen-check` gates drift and runs from `make lint`.
- Use the `useGraphqlApi` hook (`hooks/use-graphql-api.ts`) for document
  queries/mutations. The name and the GraphQL-shaped operations are retained for
  continuity; the transport is REST.
- **There are no subscriptions.** Status updates come from polling: the
  `usePolling` hook (`hooks/use-polling.ts`) runs a callback on an interval and
  **pauses while the browser tab is hidden**, firing immediately when it becomes
  visible again. Document list ~5 s, open document ~4 s until terminal status,
  circuit breaker ~15 s. When you add a live-updating view, add a poll — do not
  look for a subscription to hook into.
- **Chat tokens do stream**, but not through the REST API: the browser reads a
  Lambda Function URL (`ChatStreamProcessorUrl`, `InvokeMode=RESPONSE_STREAM`,
  `AuthType=AWS_IAM`) directly, SigV4-signing with the Cognito Identity Pool
  credentials. See `src/api/stream-client.ts`.
- Authorization is **not** enforced at the API edge. The Cognito authorizer only
  authenticates; each resolver re-checks the caller's Cognito groups. A UI change
  that exposes a new operation still needs the server-side group check and an
  entry in the RBAC baseline — see `.claude/skills/api-rbac-test.md`.

## Key Dependencies
| Package | Purpose |
|---------|---------|
| `@cloudscape-design/components` v3 | UI components |
| `@cloudscape-design/collection-hooks` | Table/collection utilities |
| `@cloudscape-design/chat-components` | Chat UI |
| `@monaco-editor/react` | Code editor |
| `chart.js` + `react-chartjs-2` | Charts |
| `recharts` | Alternative charts |
| `react-markdown` + `remark-gfm` | Markdown rendering |
| `@dnd-kit/core` + `@dnd-kit/sortable` | Drag and drop |
| `pdfjs-dist` | PDF rendering |
| `dompurify` | HTML sanitization |
| `immer` | Immutable state updates |

## ESLint / Prettier Config
- **Flat config** (`eslint.config.js`)
- **Prettier**: `printWidth: 140`, `singleQuote: true`, `trailingComma: 'all'`
- **Max line length**: 140 (ignoring URLs, templates, comments, strings)
- **Components**: Arrow functions enforced (`react/function-component-definition`)
- **Unused vars**: Warn (ignore `_` prefixed)
- **No explicit any**: Warn
- **react-hooks/exhaustive-deps**: OFF (intentionally disabled)
- **Linebreak**: Unix only

## Hook File Naming
- NEW hooks: kebab-case (`use-my-hook.ts`)
- Legacy hooks: camelCase (`useMyHook.ts`) — don't rename existing ones

## CSS
- CSS Modules with `camelCase` convention
- Import Cloudscape global styles: `import '@cloudscape-design/global-styles/index.css'`

## Commands
```bash
make ui-lint             # Lint + typecheck (checksum-cached; a cache hit prints
                         # "⏭️  UI lint SKIPPED" and runs neither eslint nor tsc.
                         # FORCE=1 runs them. `make lint-cicd` sets
                         # UI_LINT_NO_SKIP=1, so that target never skips)
make ui-build            # Production build
make ui-start STACK_NAME=<name>   # Dev server on port 3000
make codegen             # Regenerate GraphQL types
make codegen-check       # Verify generated types are up to date
cd src/ui && npm run lint -- --fix   # Direct ESLint fix
cd src/ui && npm run typecheck       # Direct TypeScript check
```
