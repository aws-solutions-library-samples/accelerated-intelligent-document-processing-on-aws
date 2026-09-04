import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import remarkGithubVideo from "./plugins/remark-github-video.mjs";
import remarkRewriteDocsLinks from "./plugins/remark-rewrite-docs-links.mjs";

export default defineConfig({
  site: "https://aws-solutions-library-samples.github.io",
  base: "/accelerated-intelligent-document-processing-on-aws",
  markdown: {
    remarkPlugins: [remarkGithubVideo, remarkRewriteDocsLinks],
  },
  integrations: [
    starlight({
      title: "GenAI IDP",
      description:
        "GenAI Intelligent Document Processing — scalable, serverless AWS solution for automated document processing",
      logo: {
        dark: "./src/assets/logo-dark.svg",
        light: "./src/assets/logo-light.svg",
        replacesTitle: false,
      },
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws",
        },
      ],
      customCss: ["./src/styles/custom.css"],
      sidebar: [
        {
          label: "Overview",
          items: [
            { label: "Welcome", slug: "index" },
            {
              label: "Blogs, Customer Stories & Research",
              slug: "references",
            },
          ],
        },
        {
          label: "Core",
          items: [
            { label: "Architecture", slug: "architecture" },
            { label: "Quick Start", slug: "quick-start" },
            { label: "Deployment", slug: "deployment" },
            { label: "AI-Guided Deployment Walkthrough", slug: "idp-deployment-ai-guide" },
            { label: "Headless Deployment", slug: "headless-deployment" },
            { label: "API Gateway Hosting", slug: "apigateway-hosting" },
            { label: "Private Network Deployment", slug: "deployment-private-network" },
            { label: "VPC-Secured Mode", slug: "vpc-secured-mode" },
            { label: "Configuration", slug: "configuration" },
            {
              label: "Configuration Profiles",
              slug: "configuration-profiles",
            },
            // Redirect stub for the page's old name, kept so existing links and
            // bookmarks do not 404. Listed next to its replacement, and labelled
            // so the nav does not read as two competing pages. sync-sidebar.mjs
            // adds any unlisted doc automatically, so leaving it out entirely
            // just means it lands in "New & Uncategorized" on the next run.
            {
              label: "Configuration Versions (renamed)",
              slug: "configuration-versions",
            },
            {
              label: "Configuration Best Practices",
              slug: "idp-configuration-best-practices",
            },
            {
              label: "JSON Schema Migration",
              slug: "json-schema-migration",
            },
            { label: "Web UI", slug: "web-ui" },
            {
              label: "UI ⇄ Backend Transport (AppSync → REST)",
              slug: "migration-appsync-to-rest",
            },
            { label: "IDP CLI", slug: "idp-cli" },
            { label: "IDP SDK", slug: "idp-sdk" },
            { label: "idp_common API Reference", slug: "idpcommon-api-reference" },
            { label: "Demo Videos", slug: "demo-videos" },
            { label: "Troubleshooting", slug: "troubleshooting" },
            { label: "Error Analyzer", slug: "error-analyzer" },
          ],
        },
        {
          label: "Processing Modes",
          items: [
            { label: "BDA Mode Reference", slug: "pattern-1" },
            { label: "Pipeline Mode Reference", slug: "pattern-2" },
            { label: "Discovery", slug: "discovery" },
            { label: "Policy Discovery", slug: "policy-discovery" },
          ],
        },
        {
          label: "Document Processing Features",
          items: [
            { label: "Classification", slug: "classification" },
            {
              label: "Extraction & Confidence",
              slug: "extraction-and-confidence",
            },
            // Legacy guides consolidated into Extraction & Confidence (kept as
            // redirect stubs so old deep links still resolve).
            { label: "Extraction (moved)", slug: "extraction" },
            { label: "Assessment (moved)", slug: "assessment" },
            {
              label: "Assessment Bounding Boxes (moved)",
              slug: "assessment-bounding-boxes",
            },
            { label: "Few-Shot Examples", slug: "few-shot-examples" },
            { label: "Human-in-the-Loop Review", slug: "human-review" },
            { label: "Rule Validation", slug: "rule-validation" },
            { label: "Criteria Validation", slug: "criteria-validation" },
            { label: "Missing Page Handling", slug: "missing-page-handling" },
            {
              label: "OCR Image Sizing Guide",
              slug: "ocr-image-sizing-guide",
            },
            { label: "Languages", slug: "languages" },
          ],
        },
        {
          label: "Evaluation & Testing",
          items: [
            { label: "Evaluation Framework", slug: "evaluation" },
            {
              label: "Enhanced Reporting",
              slug: "evaluation-enhanced-reporting",
            },
            { label: "Test Studio", slug: "test-studio" },
            { label: "Creating Custom Test Sets", slug: "creating-custom-test-sets" },
            { label: "MLflow Experiment Tracking", slug: "mlflow-integration" },
            {
              label: "Release Validation",
              // README.md is the index; per-release vX.Y.Z.md entries auto-list.
              items: [{ autogenerate: { directory: "release-validation" } }],
            },
          ],
        },
        {
          label: "Benchmarks & Performance",
          items: [
            { label: "Benchmarking Guide", slug: "benchmarking" },
            { label: "Configuration Guidance", slug: "benchmarking/config-guidance" },
            {
              label: "Classification Confidence",
              slug: "benchmarking/classification-confidence",
            },
            {
              label: "Multi-instance Sections",
              slug: "benchmarking/feature-multi-instance",
            },
            {
              label: "Release Audit Trail",
              // README.md is the index; per-release vX.Y.Z.md entries auto-list.
              items: [{ autogenerate: { directory: "benchmarking/releases" } }],
            },
            { label: "Extraction Scaling Guide", slug: "extraction-scaling-guide" },
          ],
        },
        {
          label: "AI Agents & Analytics",
          items: [
            { label: "Agent Analysis", slug: "agent-analysis" },
            { label: "Agent Companion Chat", slug: "agent-companion-chat" },
            { label: "Code Intelligence", slug: "code-intelligence" },
            { label: "Knowledge Base", slug: "knowledge-base" },
            { label: "Custom MCP Agent", slug: "custom-mcp-agent" },
            { label: "MCP Connector", slug: "mcp-connector" },
            { label: "MCP Server", slug: "mcp-server" },
          ],
        },
        {
          label: "Integration & Extensions",
          items: [
            { label: "Feature Platform", slug: "feature-platform" },
            {
              label: "Feature Platform Developer Guide",
              slug: "feature-platform-developer-guide",
            },
            {
              // Registered as the AWS Marketplace listing's post-subscribe /
              // "Account login details" URL. Buyers land here straight from
              // Marketplace, so this slug is effectively a public contract —
              // renaming it breaks the listing until the seller updates it.
              label: "After Subscribing on AWS Marketplace",
              slug: "marketplace-subscription-next-steps",
            },
            {
              label: "Extensions",
              items: [
                {
                  label: "Auto Optimizer (Beta)",
                  slug: "extensions/auto-optimizer",
                },
                {
                  label: "PII Anonymization",
                  slug: "extensions/pii-anonymizer",
                },
                {
                  label: "Test Set - ConfBench",
                  slug: "extensions/confbench-testset",
                },
                {
                  label: "Test Set Generator",
                  slug: "extensions/idp-data-generator",
                },
                {
                  label: "Sample: Document Status (feature add-on)",
                  slug: "extensions/sample-document-status",
                },
                {
                  label: "Sample: Health Insurance Review",
                  slug: "extensions/sample-health-insurance-review",
                },
              ],
            },
            {
              label: "Document Versions",
              slug: "document-versions",
            },
            {
              label: "Post-Processing Lambda Hook",
              slug: "post-processing-lambda-hook",
            },
            {
              label: "Lambda Hook Inference",
              slug: "lambda-hook-inference",
            },
            { label: "Nova Fine-Tuning", slug: "nova-finetuning" },
            { label: "Custom Model Fine-Tuning", slug: "custom-model-finetuning" },
            { label: "Service Tiers", slug: "service-tiers" },
          ],
        },
        {
          label: "Monitoring & Operations",
          items: [
            { label: "Monitoring", slug: "monitoring" },
            { label: "Reporting Database", slug: "reporting-database" },
            { label: "Reporting SQL Layer", slug: "reporting-sql-layer" },
            { label: "Capacity Planning", slug: "capacity-planning" },
            { label: "Cost Calculator", slug: "cost-calculator" },
            { label: "Circuit Breaker", slug: "circuit-breaker" },
            { label: "Cross-Account Bedrock", slug: "cross-account-bedrock" },
            { label: "Version Update Indicator", slug: "version-update-indicator" },
          ],
        },
        {
          label: "Planning & Security",
          items: [
            {
              label: "Well-Architected Assessment",
              slug: "well-architected",
            },
            {
              label: "AWS Services & IAM Roles",
              slug: "aws-services-and-roles",
            },
            {
              label: "Role-Based Access Control (RBAC)",
              slug: "rbac",
            },
            { label: "External Identity Provider", slug: "external-idp" },
            {
              label: "GovCloud",
              collapsed: false,
              items: [
                { label: "Deployment", slug: "govcloud-deployment" },
                { label: "Architecture", slug: "govcloud-architecture" },
                { label: "Operations", slug: "govcloud-operations" },
                { label: "Batch API", slug: "govcloud-batch-api" },
              ],
            },
            {
              label: "EU Region Model Support",
              slug: "eu-region-model-support",
            },
            { label: "OpenAI GPT-5.x Models", slug: "openai-models" },
          ],
        },
        {
          label: "Development Setup",
          items: [
            { label: "Setup: Linux", slug: "setup-development-env-linux" },
            { label: "Setup: macOS", slug: "setup-development-env-macos" },
            { label: "Setup: WSL", slug: "setup-development-env-wsl" },
            { label: "Setup: Windows", slug: "setup-development-env-windows" },
            {
              label: "Using Notebooks",
              slug: "using-notebooks-with-idp-common",
            },
            { label: "Dependency Mirroring", slug: "dependency-mirroring" },
            {
              label: "Installing First-Party Packages",
              slug: "dependency-confusion",
            },
          ],
        },
        {
          label: "Migration",
          items: [
            { label: "v0.5 → v0.6 Migration", slug: "migration-v05-to-v06" },
            { label: "v0.4 → v0.5 Migration", slug: "migration-v04-to-v05" },
            {
              label: "Granular Assessment Retirement",
              slug: "migration-granular-retirement",
            },
          ],
        },
        // AUTO-SIDEBAR-START
        {
          label: "New & Uncategorized",
          items: [
            { label: "Rule Validation Z3", slug: "rule-validation-z3" },
          ],
        },
        // AUTO-SIDEBAR-END
      ],
    }),
  ],
  // Disable image optimization for content images (our docs reference ../images/ which are symlinked)
  image: {
    service: {
      entrypoint: "astro/assets/services/noop",
    },
  },
});
