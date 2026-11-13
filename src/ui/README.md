Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# How to run IDP UI Locally:

## Quick Start (Recommended)

From the repository root, use the `make ui-start` target which automatically generates the `.env` file from your deployed stack:

```bash
make ui-start STACK_NAME=your-stack-name
```

This command will:
1. Retrieve the `WebUITestEnvFile` output from your CloudFormation stack
2. Create/update the `.env` file in `/src/ui/`
3. Install npm dependencies if needed
4. Start the development server

Open [http://localhost:3000](http://localhost:3000) to view it in the browser.

## Manual Setup

If you prefer to set up manually:

1. To get the IDP UI to run locally, you will need to configure a `.env` file in the `/src/ui/` folder. The contents of the `.env` file must contain the environment variables listed below along with their values.

The values may be found in the Outputs of the GenAIIDP stack under `WebUITestEnvFile`.
Example contents of `.env` file:

```
  VITE_USER_POOL_ID=us-west-2_XXXXXXXXX
  VITE_USER_POOL_CLIENT_ID=XXXXXXXXXXXXXXXXXXXXXXXXXX
  VITE_IDENTITY_POOL_ID=us-west-2:XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
  VITE_API_BASE_URL=https://xxxxxxxxxx.execute-api.us-west-2.amazonaws.com/api
  VITE_STREAM_URL=https://xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.lambda-url.us-west-2.on.aws/
  VITE_AWS_REGION=us-west-2
  VITE_SETTINGS_PARAMETER=CFN-IDPSettingsParameter-XXXXXXXXXXXX
```

The two transport variables are the ones most easily mistyped. `VITE_API_BASE_URL`
is the base URL of the API Gateway REST API (the `api` stage) that the UI reads in
`src/api/rest-client.ts`; every operation is a `POST` to
`${VITE_API_BASE_URL}/op/<fieldName>`. `VITE_STREAM_URL` is the Lambda **Function
URL** of the chat-streaming function, read in `src/api/stream-client.ts` and
SigV4-signed with Cognito Identity Pool credentials — note that a Function URL is
shaped `https://<id>.lambda-url.<region>.on.aws/`, not like an API Gateway URL.

The `WebUITestEnvFile` output contains several further variables beyond the ones
shown above (external-IdP settings, bucket names, and default token/latency
values). Copying the whole output — which is what `make ui-start` does — is more
reliable than hand-picking lines.

2. Make sure you have NodeJS 22.12+ installed.
3. Run `npm install` to install dependencies
4. Run `npm run start` to start the local development mode.
5. Open [http://localhost:3000](http://localhost:3000) to view it in the browser.

Default React App Readme Below:

---

# Getting Started with Create React App

This project was bootstrapped with [Create React App](https://github.com/facebook/create-react-app).

## Available Scripts

In the project directory, you can run:

### `npm start`

Runs the app in the development mode.\
Open [http://localhost:3000](http://localhost:3000) to view it in the browser.

The page will reload if you make edits.\
You will also see any lint errors in the console.

### `npm test`

Launches the test runner in the interactive watch mode.\
See the section about [running tests](https://facebook.github.io/create-react-app/docs/running-tests) for more information.

### `npm run build`

Builds the app for production to the `build` folder.\
It correctly bundles React in production mode and optimizes the build for the best performance.

The build is minified and the filenames include the hashes.\
Your app is ready to be deployed!

See the section about [deployment](https://facebook.github.io/create-react-app/docs/deployment) for more information.

### `npm run eject`

**Note: this is a one-way operation. Once you `eject`, you can’t go back!**

If you aren’t satisfied with the build tool and configuration choices, you can `eject` at any time. This command will remove the single build dependency from your project.

Instead, it will copy all the configuration files and the transitive dependencies (webpack, Babel, ESLint, etc) right into your project so you have full control over them. All of the commands except `eject` will still work, but they will point to the copied scripts so you can tweak them. At this point you’re on your own.

You don’t have to ever use `eject`. The curated feature set is suitable for small and middle deployments, and you shouldn’t feel obligated to use this feature. However we understand that this tool wouldn’t be useful if you couldn’t customize it when you are ready for it.

## Learn More

You can learn more in the [Create React App documentation](https://facebook.github.io/create-react-app/docs/getting-started).

To learn React, check out the [React documentation](https://reactjs.org/).

### Code Splitting

This section has moved here: [https://facebook.github.io/create-react-app/docs/code-splitting](https://facebook.github.io/create-react-app/docs/code-splitting)

### Analyzing the Bundle Size

This section has moved here: [https://facebook.github.io/create-react-app/docs/analyzing-the-bundle-size](https://facebook.github.io/create-react-app/docs/analyzing-the-bundle-size)

### Making a Progressive Web App

This section has moved here: [https://facebook.github.io/create-react-app/docs/making-a-progressive-web-app](https://facebook.github.io/create-react-app/docs/making-a-progressive-web-app)

### Advanced Configuration

This section has moved here: [https://facebook.github.io/create-react-app/docs/advanced-configuration](https://facebook.github.io/create-react-app/docs/advanced-configuration)

### Deployment

This section has moved here: [https://facebook.github.io/create-react-app/docs/deployment](https://facebook.github.io/create-react-app/docs/deployment)

### `npm run build` fails to minify

This section has moved here: [https://facebook.github.io/create-react-app/docs/troubleshooting#npm-run-build-fails-to-minify](https://facebook.github.io/create-react-app/docs/troubleshooting#npm-run-build-fails-to-minify)