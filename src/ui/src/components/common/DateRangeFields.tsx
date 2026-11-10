// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// A UTC date and time range (start and end), with the 365-day bound the document
// lists and bulk Skip All Reviews share. Used by DateRangeModal and BulkSkipModal.
import React from 'react';
import { DatePicker, FormField, SpaceBetween, TimeInput } from '@cloudscape-design/components';

export interface DateRangeValue {
  startDate: string;
  startTime: string;
  endDate: string;
  endTime: string;
}

export const MAX_RANGE_DAYS = 365;

/** The last ``days`` days, UTC, 00:00:00 to 23:59:59. */
export const lastDaysRange = (days = 7, now: Date = new Date()): DateRangeValue => {
  const from = new Date(now.getTime() - days * 24 * 60 * 60 * 1000);
  return {
    startDate: from.toISOString().split('T')[0],
    startTime: '00:00:00',
    endDate: now.toISOString().split('T')[0],
    endTime: '23:59:59',
  };
};

/** ``{startDateTime, endDateTime}`` as UTC ISO strings (``...000Z``). */
export const toIsoRange = (value: DateRangeValue): { startDateTime: string; endDateTime: string } => ({
  startDateTime: `${value.startDate}T${value.startTime || '00:00:00'}.000Z`,
  endDateTime: `${value.endDate}T${value.endTime || '23:59:59'}.000Z`,
});

/** Why the range is not usable, or null. */
export const validateDateRange = (value: DateRangeValue, maxDays = MAX_RANGE_DAYS): string | null => {
  if (!value.startDate || !value.endDate) return 'Both start and end dates are required.';
  const { startDateTime, endDateTime } = toIsoRange(value);
  if (startDateTime >= endDateTime) return 'Start date/time must be before end date/time.';
  const days = (new Date(endDateTime).getTime() - new Date(startDateTime).getTime()) / (1000 * 60 * 60 * 24);
  if (days > maxDays) return `Date range cannot exceed ${maxDays} days. Please select a shorter range.`;
  return null;
};

interface DateRangeFieldsProps {
  value: DateRangeValue;
  onChange: (value: DateRangeValue) => void;
}

const DateRangeFields = ({ value, onChange }: DateRangeFieldsProps): React.JSX.Element => (
  <SpaceBetween size="m">
    <SpaceBetween size="m" direction="horizontal">
      <FormField label="Start date" constraintText="YYYY/MM/DD">
        <DatePicker
          value={value.startDate}
          onChange={({ detail }) => onChange({ ...value, startDate: detail.value })}
          placeholder="YYYY/MM/DD"
          openCalendarAriaLabel={(selectedDate) => `Choose start date${selectedDate ? `, selected date is ${selectedDate}` : ''}`}
        />
      </FormField>
      <FormField label="Start time (UTC)" constraintText="HH:mm:ss">
        <TimeInput
          value={value.startTime}
          onChange={({ detail }) => onChange({ ...value, startTime: detail.value })}
          format="hh:mm:ss"
          placeholder="00:00:00"
        />
      </FormField>
    </SpaceBetween>
    <SpaceBetween size="m" direction="horizontal">
      <FormField label="End date" constraintText="YYYY/MM/DD">
        <DatePicker
          value={value.endDate}
          onChange={({ detail }) => onChange({ ...value, endDate: detail.value })}
          placeholder="YYYY/MM/DD"
          openCalendarAriaLabel={(selectedDate) => `Choose end date${selectedDate ? `, selected date is ${selectedDate}` : ''}`}
        />
      </FormField>
      <FormField label="End time (UTC)" constraintText="HH:mm:ss">
        <TimeInput
          value={value.endTime}
          onChange={({ detail }) => onChange({ ...value, endTime: detail.value })}
          format="hh:mm:ss"
          placeholder="23:59:59"
        />
      </FormField>
    </SpaceBetween>
  </SpaceBetween>
);

export default DateRangeFields;
