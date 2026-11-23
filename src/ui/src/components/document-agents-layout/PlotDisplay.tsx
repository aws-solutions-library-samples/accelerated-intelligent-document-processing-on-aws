// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useState, useEffect } from 'react';
import { Box, Container, Header, Select, SpaceBetween } from '@cloudscape-design/components';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
  ArcElement,
  Filler,
} from 'chart.js';
import { Bar, Line, Pie, Doughnut } from 'react-chartjs-2';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import { formattingLocale, useFormatters } from '../../i18n/format';

// Register Chart.js components
ChartJS.register(CategoryScale, LinearScale, BarElement, LineElement, PointElement, Title, Tooltip, Legend, ArcElement, Filler);

const messages = defineMessages<{
  typeBar: NoMessageValues;
  typeLine: NoMessageValues;
  typePie: NoMessageValues;
  typeDoughnut: NoMessageValues;
  combinedData: NoMessageValues;
  unknownLabel: NoMessageValues;
  invalidData: NoMessageValues;
  invalidLabels: NoMessageValues;
  renderError: { readonly chartType: string };
  debugInfo: NoMessageValues;
  defaultTitle: NoMessageValues;
  typePlaceholder: NoMessageValues;
}>({
  typeBar: {
    id: 'agents.plot.type.bar',
    defaultMessage: 'Bar Chart',
    description: "Chart type option in the picker above an agent's chart.",
  },
  typeLine: {
    id: 'agents.plot.type.line',
    defaultMessage: 'Line Chart',
    description: "Chart type option in the picker above an agent's chart.",
  },
  typePie: {
    id: 'agents.plot.type.pie',
    defaultMessage: 'Pie Chart',
    description: "Chart type option in the picker above an agent's chart.",
  },
  typeDoughnut: {
    id: 'agents.plot.type.doughnut',
    defaultMessage: 'Doughnut Chart',
    description: "Chart type option in the picker above an agent's chart.",
  },
  combinedData: {
    id: 'agents.plot.combinedData',
    defaultMessage: 'Combined Data',
    description: 'Name of the one data series a pie or doughnut chart shows when it adds up several series that have no names.',
  },
  unknownLabel: {
    id: 'agents.plot.unknownLabel',
    defaultMessage: 'Unknown',
    description: 'Tooltip of a pie or doughnut chart slice that has no label, when its value cannot be shown.',
  },
  invalidData: {
    id: 'agents.plot.invalidData',
    defaultMessage: 'Invalid chart data structure',
    description: "Shown in place of an agent's chart when its data has no data series.",
  },
  invalidLabels: {
    id: 'agents.plot.invalidLabels',
    defaultMessage: 'Invalid or missing chart labels',
    description: "Shown in place of an agent's chart when its data has no labels.",
  },
  renderError: {
    id: 'agents.plot.renderError',
    defaultMessage: 'Error rendering {chartType} chart',
    description:
      "Shown in place of an agent's chart when it cannot be drawn. {chartType} is the chart type code, in lower case and as is (for example bar or pie).",
  },
  debugInfo: {
    id: 'agents.plot.debugInfo',
    defaultMessage: 'Debug Information',
    description: "Expandable section under a chart error that shows the chart's data and options.",
  },
  defaultTitle: {
    id: 'agents.plot.defaultTitle',
    defaultMessage: 'Chart',
    description: "Heading of an agent's chart when the chart has no title.",
  },
  typePlaceholder: {
    id: 'agents.plot.typePlaceholder',
    defaultMessage: 'Select chart type',
    description: "Placeholder of the chart type picker above an agent's chart.",
  },
});

interface ChartDataset {
  data: number[];
  label?: string;
  backgroundColor?: string | string[];
  borderColor?: string | string[];
  borderWidth?: number;
  [key: string]: unknown;
}

interface ChartData {
  datasets: ChartDataset[];
  labels?: (string | number)[];
}

interface PlotDataType {
  type?: string;
  data: ChartData;
  options?: Record<string, unknown>;
}

interface PlotDisplayProps {
  plotData?: PlotDataType | Record<string, unknown> | null;
}

interface ChartTypeOption {
  label: string;
  value: string;
}

const PlotDisplay = ({ plotData = null }: PlotDisplayProps): React.JSX.Element | null => {
  const intl = useIntl();
  // Chart type options for the dropdown
  const chartTypeOptions: ChartTypeOption[] = [
    { label: intl.formatMessage(messages.typeBar), value: 'bar' },
    { label: intl.formatMessage(messages.typeLine), value: 'line' },
    { label: intl.formatMessage(messages.typePie), value: 'pie' },
    { label: intl.formatMessage(messages.typeDoughnut), value: 'doughnut' },
  ];

  // State to track the current chart type, initialized with the type from JSON
  const [currentChartType, setCurrentChartType] = useState<string | null>(null);
  // The selected option is kept by its value and looked up at render, so its label follows the UI language.
  const [selectedValue, setSelectedValue] = useState<string | null>(null);
  const selectedOption = chartTypeOptions.find((option) => option.value === selectedValue) ?? null;
  const { formatPercent, locale } = useFormatters();

  // Initialize chart type when plotData changes
  useEffect(() => {
    const typedPlotData = plotData as PlotDataType | null;
    if (typedPlotData?.type) {
      const initialType = typedPlotData.type.toLowerCase();
      setCurrentChartType(initialType);

      // Find the matching option for the Select component
      const matchingOption = chartTypeOptions.find((option) => option.value === initialType);
      setSelectedValue((matchingOption || chartTypeOptions[0]).value);
    }
  }, [plotData]);

  if (!plotData) {
    return null;
  }

  const typedPlotData = plotData as PlotDataType;

  // Handle chart type change from dropdown
  const handleChartTypeChange = ({ detail }: { detail: { selectedOption: { label?: string; value?: string } } }) => {
    setCurrentChartType(detail.selectedOption.value || null);
    setSelectedValue(detail.selectedOption.value || null);
  };

  // Prepare chart data with potential modifications for different chart types
  const prepareChartData = (originalData: ChartData, chartType: string) => {
    const { datasets, labels } = originalData;

    // Ensure labels are strings to avoid type warnings
    const stringLabels = labels ? labels.map((label) => String(label)) : [];

    // For pie and doughnut charts, we might need to aggregate data if there are multiple datasets
    if ((chartType === 'pie' || chartType === 'doughnut') && datasets.length > 1) {
      // Aggregate all datasets into a single dataset for pie/doughnut charts
      const aggregatedData = stringLabels.map((_, index) => datasets.reduce((sum, dataset) => sum + (dataset.data[index] || 0), 0));

      return {
        labels: stringLabels,
        datasets: [
          {
            data: aggregatedData,
            backgroundColor: [
              'rgba(255, 99, 132, 0.8)',
              'rgba(54, 162, 235, 0.8)',
              'rgba(255, 205, 86, 0.8)',
              'rgba(75, 192, 192, 0.8)',
              'rgba(153, 102, 255, 0.8)',
              'rgba(255, 159, 64, 0.8)',
              'rgba(255, 193, 7, 0.8)',
              'rgba(76, 175, 80, 0.8)',
              'rgba(156, 39, 176, 0.8)',
              'rgba(96, 125, 139, 0.8)',
            ],
            borderColor: [
              'rgba(255, 99, 132, 1)',
              'rgba(54, 162, 235, 1)',
              'rgba(255, 205, 86, 1)',
              'rgba(75, 192, 192, 1)',
              'rgba(153, 102, 255, 1)',
              'rgba(255, 159, 64, 1)',
              'rgba(255, 193, 7, 1)',
              'rgba(76, 175, 80, 1)',
              'rgba(156, 39, 176, 1)',
              'rgba(96, 125, 139, 1)',
            ],
            borderWidth: 1,
            label: datasets.map((d) => d.label).join(' + ') || intl.formatMessage(messages.combinedData),
          },
        ],
      };
    }

    // For pie and doughnut charts with single dataset, ensure proper color arrays
    if ((chartType === 'pie' || chartType === 'doughnut') && datasets.length === 1) {
      const dataset = datasets[0];
      const dataLength = dataset.data.length;

      // Generate colors if not provided or if there aren't enough colors
      const defaultColors = [
        'rgba(255, 99, 132, 0.8)',
        'rgba(54, 162, 235, 0.8)',
        'rgba(255, 205, 86, 0.8)',
        'rgba(75, 192, 192, 0.8)',
        'rgba(153, 102, 255, 0.8)',
        'rgba(255, 159, 64, 0.8)',
        'rgba(255, 193, 7, 0.8)',
        'rgba(76, 175, 80, 0.8)',
        'rgba(156, 39, 176, 0.8)',
        'rgba(96, 125, 139, 0.8)',
      ];

      const defaultBorderColors = [
        'rgba(255, 99, 132, 1)',
        'rgba(54, 162, 235, 1)',
        'rgba(255, 205, 86, 1)',
        'rgba(75, 192, 192, 1)',
        'rgba(153, 102, 255, 1)',
        'rgba(255, 159, 64, 1)',
        'rgba(255, 193, 7, 1)',
        'rgba(76, 175, 80, 1)',
        'rgba(156, 39, 176, 1)',
        'rgba(96, 125, 139, 1)',
      ];

      return {
        labels: stringLabels,
        datasets: [
          {
            ...dataset,
            backgroundColor:
              dataset.backgroundColor && Array.isArray(dataset.backgroundColor) && dataset.backgroundColor.length >= dataLength
                ? dataset.backgroundColor
                : defaultColors.slice(0, dataLength),
            borderColor:
              dataset.borderColor && Array.isArray(dataset.borderColor) && dataset.borderColor.length >= dataLength
                ? dataset.borderColor
                : defaultBorderColors.slice(0, dataLength),
            borderWidth: dataset.borderWidth || 1,
          },
        ],
      };
    }

    return { ...originalData, labels: stringLabels };
  };

  // Prepare chart options with potential modifications for different chart types
  const prepareChartOptions = (originalOptions: Record<string, unknown> | undefined, chartType: string) => {
    const baseOptions: Record<string, unknown> = {
      responsive: true,
      maintainAspectRatio: false,
      // Chart.js formats axis and tooltip numbers in this locale; under en it is undefined, the browser default, as before.
      locale: formattingLocale(locale),
      ...originalOptions,
    };

    // Ensure plugins object exists
    if (!baseOptions.plugins) {
      baseOptions.plugins = {};
    }

    const plugins = baseOptions.plugins as Record<string, unknown>;

    // For pie and doughnut charts, we typically don't need scales
    if (chartType === 'pie' || chartType === 'doughnut') {
      const { scales: _scales, ...optionsWithoutScales } = baseOptions;

      // Create a clean legend configuration for pie charts
      const legendConfig = {
        display: true,
        position: 'right',
        labels: {
          usePointStyle: true,
          padding: 15,
          boxWidth: 12,
          font: {
            size: 12,
          },
        },
      };

      // Safely merge tooltip configuration
      const existingTooltip = (plugins?.tooltip || {}) as Record<string, unknown>;
      const existingCallbacks = (existingTooltip.callbacks || {}) as Record<string, unknown>;

      return {
        ...optionsWithoutScales,
        plugins: {
          ...plugins,
          legend: legendConfig,
          tooltip: {
            enabled: true,
            ...existingTooltip,
            callbacks: {
              ...existingCallbacks,
              label(context: Record<string, unknown>) {
                try {
                  const label = (context.label as string) || '';
                  const value = (context.parsed as number) || 0;
                  const dataset = context.dataset as { data: number[] };
                  const total = dataset.data.reduce((sum: number, val: number) => sum + val, 0);
                  const percentage =
                    total > 0 ? formatPercent(value / total, { fractionDigits: 1 }) : formatPercent(0, { fractionDigits: 0 });
                  return `${label}: ${value} (${percentage})`;
                } catch (error) {
                  console.error('Error in tooltip callback:', error);
                  return (context.label as string) || intl.formatMessage(messages.unknownLabel);
                }
              },
            },
          },
        },
      };
    }

    // For other chart types, ensure tooltip callbacks are properly structured
    const tooltip = plugins?.tooltip as Record<string, unknown> | undefined;
    if (tooltip?.callbacks) {
      const existingCallbacks = tooltip.callbacks as Record<string, unknown>;

      // Validate that callbacks are functions
      Object.keys(existingCallbacks).forEach((callbackName) => {
        if (typeof existingCallbacks[callbackName] !== 'function') {
          console.warn(`Invalid tooltip callback '${callbackName}' - not a function`);
          delete existingCallbacks[callbackName];
        }
      });
    }

    return baseOptions;
  };

  const renderChart = (): React.JSX.Element | null => {
    if (!currentChartType) return null;

    const { data, options } = typedPlotData;

    // Validate data structure
    if (!data || !data.datasets || !Array.isArray(data.datasets) || data.datasets.length === 0) {
      return (
        <div style={{ padding: '20px', textAlign: 'center', color: 'orange' }}>
          <p>
            <FormattedMessage {...messages.invalidData} />
          </p>
        </div>
      );
    }

    if (!data.labels || !Array.isArray(data.labels) || data.labels.length === 0) {
      return (
        <div style={{ padding: '20px', textAlign: 'center', color: 'orange' }}>
          <p>
            <FormattedMessage {...messages.invalidLabels} />
          </p>
        </div>
      );
    }

    // Add debugging for pie chart issues
    if (currentChartType === 'pie' || currentChartType === 'doughnut') {
      console.log('Pie/Doughnut chart data:', data);
      console.log('Pie/Doughnut chart options:', options);
    }

    const chartData = prepareChartData(data, currentChartType);
    const chartOptions = prepareChartOptions(options, currentChartType);

    // Additional debugging for prepared data
    if (currentChartType === 'pie' || currentChartType === 'doughnut') {
      console.log('Prepared chart data:', chartData);
      console.log('Prepared chart options:', chartOptions);
    }

    const chartProps = {
      data: chartData,
      options: chartOptions,
    };

    try {
      switch (currentChartType) {
        case 'bar':
          return <Bar data={chartProps.data as never} options={chartProps.options as never} />;
        case 'line':
          return <Line data={chartProps.data as never} options={chartProps.options as never} />;
        case 'pie':
          return <Pie data={chartProps.data as never} options={chartProps.options as never} />;
        case 'doughnut':
          return <Doughnut data={chartProps.data as never} options={chartProps.options as never} />;
        default:
          return <Bar data={chartProps.data as never} options={chartProps.options as never} />; // Default to bar chart
      }
    } catch (error) {
      console.error('Chart rendering error:', error);
      return (
        <div style={{ padding: '20px', textAlign: 'center', color: 'red' }}>
          <p>
            <FormattedMessage {...messages.renderError} values={{ chartType: currentChartType }} />
          </p>
          <p>{(error as Error).message}</p>
          <details style={{ marginTop: '10px', textAlign: 'left' }}>
            <summary>
              <FormattedMessage {...messages.debugInfo} />
            </summary>
            <pre style={{ fontSize: '10px', maxHeight: '200px', overflow: 'auto' }}>
              {JSON.stringify({ data: chartData, options: chartOptions }, null, 2)}
            </pre>
          </details>
        </div>
      );
    }
  };

  const titleText = (typedPlotData.options as Record<string, unknown> | undefined)?.title as Record<string, unknown> | undefined;

  return (
    <Container header={<Header variant="h3">{(titleText?.text as string) || <FormattedMessage {...messages.defaultTitle} />}</Header>}>
      <Box padding="m">
        <SpaceBetween direction="vertical" size="m">
          {/* Chart type selector */}
          <Box float="right">
            <Select
              selectedOption={selectedOption}
              onChange={handleChartTypeChange}
              options={chartTypeOptions}
              placeholder={intl.formatMessage(messages.typePlaceholder)}
              expandToViewport
            />
          </Box>

          {/* Chart display */}
          <div style={{ height: '400px', width: '100%' }}>{renderChart()}</div>
        </SpaceBetween>
      </Box>
    </Container>
  );
};

export default PlotDisplay;
