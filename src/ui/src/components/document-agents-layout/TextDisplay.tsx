// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { Box, Container, Header } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type NoMessageValues } from 'react-intl';
import SafeMarkdown from '../common/SafeMarkdown';

const messages = defineMessages<{ title: NoMessageValues }>({
  title: {
    id: 'agents.text.title',
    defaultMessage: 'Text Response',
    description: "Heading of an agent's text result.",
  },
});

interface ChildrenProps {
  children?: React.ReactNode;
}

// Custom heading components with stronger inline styles
const H1Component = ({ children }: ChildrenProps): React.JSX.Element => (
  <h1
    style={{
      fontSize: '1.75em',
      fontWeight: 'bold',
      color: '#232f3e',
      marginTop: '0.5em',
      marginBottom: '0.5em',
      lineHeight: '1.2',
    }}
  >
    {children}
  </h1>
);

const H2Component = ({ children }: ChildrenProps): React.JSX.Element => (
  <h2
    style={{
      fontSize: '1.5em',
      fontWeight: 'bold',
      color: '#232f3e',
      marginTop: '0.75em',
      marginBottom: '0.5em',
      lineHeight: '1.3',
    }}
  >
    {children}
  </h2>
);

const H3Component = ({ children }: ChildrenProps): React.JSX.Element => (
  <h3
    style={{
      fontSize: '1.25em',
      fontWeight: 'bold',
      color: '#232f3e',
      marginTop: '0.75em',
      marginBottom: '0.5em',
      lineHeight: '1.3',
    }}
  >
    {children}
  </h3>
);

const H4Component = ({ children }: ChildrenProps): React.JSX.Element => (
  <h4
    style={{
      fontSize: '1.1em',
      fontWeight: 'bold',
      color: '#232f3e',
      marginTop: '0.75em',
      marginBottom: '0.5em',
      lineHeight: '1.3',
    }}
  >
    {children}
  </h4>
);

const ParagraphComponent = ({ children }: ChildrenProps): React.JSX.Element => (
  <p
    style={{
      marginBottom: '1em',
      lineHeight: '1.6',
      color: '#16191f',
    }}
  >
    {children}
  </p>
);

interface CodeComponentProps {
  inline?: boolean;
  children?: React.ReactNode;
}

const CodeComponent = ({ inline = false, children }: CodeComponentProps): React.JSX.Element => {
  if (inline) {
    return (
      <code
        style={{
          backgroundColor: '#f4f4f4',
          padding: '0.2em 0.4em',
          borderRadius: '3px',
          fontFamily: 'Monaco, Consolas, "Courier New", monospace',
          fontSize: '0.9em',
          color: '#d63384',
        }}
      >
        {children}
      </code>
    );
  }
  return (
    <code
      style={{
        fontFamily: 'Monaco, Consolas, "Courier New", monospace',
        fontSize: '0.9em',
      }}
    >
      {children}
    </code>
  );
};

const PreComponent = ({ children }: ChildrenProps): React.JSX.Element => (
  <pre
    style={{
      backgroundColor: '#f8f9fa',
      border: '1px solid #e9ecef',
      padding: '1em',
      borderRadius: '5px',
      overflow: 'auto',
      marginBottom: '1em',
      fontFamily: 'Monaco, Consolas, "Courier New", monospace',
      fontSize: '0.9em',
    }}
  >
    {children}
  </pre>
);

const UlComponent = ({ children }: ChildrenProps): React.JSX.Element => (
  <ul style={{ marginBottom: '1em', paddingLeft: '2em' }}>{children}</ul>
);

const OlComponent = ({ children }: ChildrenProps): React.JSX.Element => (
  <ol style={{ marginBottom: '1em', paddingLeft: '2em' }}>{children}</ol>
);

const LiComponent = ({ children }: ChildrenProps): React.JSX.Element => <li style={{ marginBottom: '0.25em' }}>{children}</li>;

const BlockquoteComponent = ({ children }: ChildrenProps): React.JSX.Element => (
  <blockquote
    style={{
      borderLeft: '4px solid #0073bb',
      paddingLeft: '1em',
      marginLeft: '0',
      marginBottom: '1em',
      fontStyle: 'italic',
      color: '#5f6368',
    }}
  >
    {children}
  </blockquote>
);

const TableComponent = ({ children }: ChildrenProps): React.JSX.Element => (
  <table
    style={{
      borderCollapse: 'collapse',
      width: '100%',
      marginBottom: '1em',
      border: '1px solid #ddd',
    }}
  >
    {children}
  </table>
);

const ThComponent = ({ children }: ChildrenProps): React.JSX.Element => (
  <th
    style={{
      border: '1px solid #ddd',
      padding: '0.5em',
      textAlign: 'left',
      backgroundColor: '#f2f2f2',
      fontWeight: 'bold',
    }}
  >
    {children}
  </th>
);

const TdComponent = ({ children }: ChildrenProps): React.JSX.Element => (
  <td
    style={{
      border: '1px solid #ddd',
      padding: '0.5em',
      textAlign: 'left',
    }}
  >
    {children}
  </td>
);

interface LinkComponentProps {
  children?: React.ReactNode;
  href?: string;
}

const LinkComponent = ({ children, href = '#' }: LinkComponentProps): React.JSX.Element => (
  <a
    href={href}
    style={{
      color: '#0073bb',
      textDecoration: 'none',
    }}
    onMouseEnter={(e) => {
      (e.target as HTMLAnchorElement).style.textDecoration = 'underline';
    }}
    onMouseLeave={(e) => {
      (e.target as HTMLAnchorElement).style.textDecoration = 'none';
    }}
  >
    {children}
  </a>
);

const StrongComponent = ({ children }: ChildrenProps): React.JSX.Element => <strong style={{ fontWeight: 'bold' }}>{children}</strong>;

const EmComponent = ({ children }: ChildrenProps): React.JSX.Element => <em style={{ fontStyle: 'italic' }}>{children}</em>;

interface TextData {
  content: string;
  responseType?: string;
}

interface TextDisplayProps {
  textData?: TextData | Record<string, unknown> | null;
}

const TextDisplay = ({ textData = null }: TextDisplayProps): React.JSX.Element | null => {
  if (!textData || !(textData as TextData).content) {
    return null;
  }

  const markdownComponents = {
    h1: H1Component,
    h2: H2Component,
    h3: H3Component,
    h4: H4Component,
    p: ParagraphComponent,
    code: CodeComponent,
    pre: PreComponent,
    ul: UlComponent,
    ol: OlComponent,
    li: LiComponent,
    blockquote: BlockquoteComponent,
    table: TableComponent,
    th: ThComponent,
    td: TdComponent,
    a: LinkComponent,
    strong: StrongComponent,
    em: EmComponent,
  };

  return (
    <Container
      header={
        <Header variant="h3">
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      <Box padding="m">
        <Box
          variant="div"
          fontSize="body-m"
          padding="s"
          {...({ backgroundColor: 'background-container-content' } as Record<string, unknown>)}
        >
          <div style={{ lineHeight: '1.6' }}>
            <SafeMarkdown components={markdownComponents}>{(textData as TextData).content}</SafeMarkdown>
          </div>
        </Box>
      </Box>
    </Container>
  );
};

export default TextDisplay;
