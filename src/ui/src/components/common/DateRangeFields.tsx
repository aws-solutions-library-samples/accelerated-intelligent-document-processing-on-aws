// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// A UTC date and time range (start and end), with the 365-day bound the document
// lists and bulk Skip All Reviews share. Used by DateRangeModal and BulkSkipModal.
import React from 'react';
import { DatePicker, FormField, SpaceBetween, TimeInput } from '@cloudscape-design/components';
import { defineMessages, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';

const messages = defineMessages<{
  bothDatesRequired: NoMessageValues;
  startBeforeEnd: NoMessageValues;
  rangeTooLong: { readonly maxDays: number };
  startDate: NoMessageValues;
  startTime: NoMessageValues;
  endDate: NoMessageValues;
  endTime: NoMessageValues;
  timeConstraint: NoMessageValues;
  chooseStartDate: NoMessageValues;
  chooseStartDateSelected: { readonly selectedDate: string };
  chooseEndDate: NoMessageValues;
  chooseEndDateSelected: { readonly selectedDate: string };
}>({
  bothDatesRequired: {
    id: 'common.dateRangeFields.bothDatesRequired',
    defaultMessage: 'Both start and end dates are required.',
    description: 'Validation error of a UTC date range when the start or end date is empty.',
  },
  startBeforeEnd: {
    id: 'common.dateRangeFields.startBeforeEnd',
    defaultMessage: 'Start date/time must be before end date/time.',
    description: 'Validation error of a UTC date range whose start is not before its end.',
  },
  rangeTooLong: {
    id: 'common.dateRangeFields.rangeTooLong',
    defaultMessage: 'Date range cannot exceed {maxDays} days. Please select a shorter range.',
    description: 'Validation error of a UTC date range that is too long. {maxDays} is the maximum number of days (365).',
  },
  startDate: {
    id: 'common.dateRangeFields.startDate',
    defaultMessage: 'Start date',
    description: 'Label of the start date field of a date range.',
  },
  startTime: {
    id: 'common.dateRangeFields.startTime',
    defaultMessage: 'Start time (UTC)',
    description: 'Label of the start time field of a date range; times are in UTC.',
  },
  endDate: {
    id: 'common.dateRangeFields.endDate',
    defaultMessage: 'End date',
    description: 'Label of the end date field of a date range.',
  },
  endTime: {
    id: 'common.dateRangeFields.endTime',
    defaultMessage: 'End time (UTC)',
    description: 'Label of the end time field of a date range; times are in UTC.',
  },
  timeConstraint: {
    id: 'common.dateRangeFields.timeConstraint',
    defaultMessage: 'HH:mm:ss',
    description: 'Format hint under a time field: hours, minutes and seconds, 24-hour clock.',
  },
  chooseStartDate: {
    id: 'common.dateRangeFields.chooseStartDate',
    defaultMessage: 'Choose start date',
    description: 'Screen-reader label of the calendar button of the start date field, when no date is selected.',
  },
  chooseStartDateSelected: {
    id: 'common.dateRangeFields.chooseStartDateSelected',
    defaultMessage: 'Choose start date, selected date is {selectedDate}',
    description:
      'Screen-reader label of the calendar button of the start date field. {selectedDate} is the selected date as the date picker reads it.',
  },
  chooseEndDate: {
    id: 'common.dateRangeFields.chooseEndDate',
    defaultMessage: 'Choose end date',
    description: 'Screen-reader label of the calendar button of the end date field, when no date is selected.',
  },
  chooseEndDateSelected: {
    id: 'common.dateRangeFields.chooseEndDateSelected',
    defaultMessage: 'Choose end date, selected date is {selectedDate}',
    description:
      'Screen-reader label of the calendar button of the end date field. {selectedDate} is the selected date as the date picker reads it.',
  },
});

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

/** A date (``YYYY-MM-DD``) and time (``HH:mm:ss``) as a UTC ISO string (``...000Z``). */
const isoDateTime = (date: string, time: string): string => date.concat('T', time, '.000', 'Z');

/** ``{startDateTime, endDateTime}`` as UTC ISO strings (``...000Z``). */
export const toIsoRange = (value: DateRangeValue): { startDateTime: string; endDateTime: string } => ({
  startDateTime: isoDateTime(value.startDate, value.startTime || '00:00:00'),
  endDateTime: isoDateTime(value.endDate, value.endTime || '23:59:59'),
});

/** Why the range is not usable (in the caller's locale), or null. */
export const validateDateRange = (value: DateRangeValue, intl: IntlShape, maxDays = MAX_RANGE_DAYS): string | null => {
  if (!value.startDate || !value.endDate) return intl.formatMessage(messages.bothDatesRequired);
  const { startDateTime, endDateTime } = toIsoRange(value);
  if (startDateTime >= endDateTime) return intl.formatMessage(messages.startBeforeEnd);
  const days = (new Date(endDateTime).getTime() - new Date(startDateTime).getTime()) / (1000 * 60 * 60 * 24);
  if (days > maxDays) return intl.formatMessage(messages.rangeTooLong, { maxDays });
  return null;
};

interface DateRangeFieldsProps {
  value: DateRangeValue;
  onChange: (value: DateRangeValue) => void;
}

const DateRangeFields = ({ value, onChange }: DateRangeFieldsProps): React.JSX.Element => {
  const intl = useIntl();
  const timeConstraint = intl.formatMessage(messages.timeConstraint);
  return (
    <SpaceBetween size="m">
      <SpaceBetween size="m" direction="horizontal">
        <FormField label={intl.formatMessage(messages.startDate)} constraintText="YYYY/MM/DD">
          <DatePicker
            value={value.startDate}
            onChange={({ detail }) => onChange({ ...value, startDate: detail.value })}
            placeholder="YYYY/MM/DD"
            openCalendarAriaLabel={(selectedDate) =>
              selectedDate
                ? intl.formatMessage(messages.chooseStartDateSelected, { selectedDate })
                : intl.formatMessage(messages.chooseStartDate)
            }
          />
        </FormField>
        <FormField label={intl.formatMessage(messages.startTime)} constraintText={timeConstraint}>
          <TimeInput
            value={value.startTime}
            onChange={({ detail }) => onChange({ ...value, startTime: detail.value })}
            format="hh:mm:ss"
            placeholder="00:00:00"
          />
        </FormField>
      </SpaceBetween>
      <SpaceBetween size="m" direction="horizontal">
        <FormField label={intl.formatMessage(messages.endDate)} constraintText="YYYY/MM/DD">
          <DatePicker
            value={value.endDate}
            onChange={({ detail }) => onChange({ ...value, endDate: detail.value })}
            placeholder="YYYY/MM/DD"
            openCalendarAriaLabel={(selectedDate) =>
              selectedDate
                ? intl.formatMessage(messages.chooseEndDateSelected, { selectedDate })
                : intl.formatMessage(messages.chooseEndDate)
            }
          />
        </FormField>
        <FormField label={intl.formatMessage(messages.endTime)} constraintText={timeConstraint}>
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
};

export default DateRangeFields;
