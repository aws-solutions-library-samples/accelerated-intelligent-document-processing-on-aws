// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// eslint-disable import/prefer-default-export

export const SYSTEM = 'System' as const;

/**
 * File extensions accepted for document processing upload (supports backend conversion).
 *
 * `.doc` is deliberately absent. It was offered here and routed by the backend for
 * every release that advertised it, but the Word converter reads the OOXML container
 * only (python-docx) and there is no pure-Python reader for the legacy OLE2 format,
 * so such a file converted to a single blank page and completed with no content —
 * silent data loss (GitHub #829). The backend now fails a legacy `.doc` with an
 * actionable message rather than blanking it; this list stops offering it in the
 * first place. Legacy `.xls` IS supported: pandas reads BIFF via `xlrd` (#800).
 */
export const SUPPORTED_UPLOAD_EXTENSIONS = '.pdf,.png,.jpg,.jpeg,.tiff,.tif,.xlsx,.xls,.csv,.docx,.txt' as const;

/** File extensions accepted for Discovery upload (PDF and images only — no backend conversion). */
export const SUPPORTED_DISCOVERY_EXTENSIONS = '.pdf,.png,.jpg,.jpeg,.tiff,.tif' as const;

/** Human-readable label for supported processing upload formats. */
export const SUPPORTED_UPLOAD_FORMATS_LABEL =
  'Supported formats: PDF, PNG, JPEG, TIFF, Excel (XLSX/XLS), CSV, Word (DOCX), and plain text (TXT). PDF and image files are processed with Textract; spreadsheet, Word, and text files are supported through backend conversion. Legacy Word (.doc) is not supported — re-save it as .docx.' as const;

export const LANGUAGE_CODES = [
  { value: '', label: 'Choose a Language' },
  { value: 'af', label: 'Afrikaans' },
  { value: 'sq', label: 'Albanian' },
  { value: 'am', label: 'Amharic' },
  { value: 'ar', label: 'Arabic' },
  { value: 'hy', label: 'Armenian' },
  { value: 'az', label: 'Azerbaijani' },
  { value: 'bn', label: 'Bengali' },
  { value: 'bs', label: 'Bosnian' },
  { value: 'bg', label: 'Bulgarian' },
  { value: 'ca', label: 'Catalan' },
  { value: 'zh', label: 'Chinese (Simplified)' },
  { value: 'zh-TW', label: 'Chinese (Traditional)' },
  { value: 'hr', label: 'Croatian' },
  { value: 'cs', label: 'Czech' },
  { value: 'da', label: 'Danish' },
  { value: 'fa-AF', label: 'Dari' },
  { value: 'nl', label: 'Dutch' },
  { value: 'en', label: 'English' },
  { value: 'et', label: 'Estonian' },
  { value: 'fa', label: 'Farsi (Persian)' },
  { value: 'tl', label: 'Filipino, Tagalog' },
  { value: 'fi', label: 'Finnish' },
  { value: 'fr', label: 'French' },
  { value: 'fr-CA', label: 'French (Canada)' },
  { value: 'ka', label: 'Georgian' },
  { value: 'de', label: 'German' },
  { value: 'el', label: 'Greek' },
  { value: 'gu', label: 'Gujarati' },
  { value: 'ht', label: 'Haitian Creole' },
  { value: 'ha', label: 'Hausa' },
  { value: 'he', label: 'Hebrew' },
  { value: 'hi', label: 'Hindi' },
  { value: 'hu', label: 'Hungarian' },
  { value: 'is', label: 'Icelandic' },
  { value: 'id', label: 'Indonesian' },
  { value: 'ga', label: 'Irish' },
  { value: 'it', label: 'Italian' },
  { value: 'ja', label: 'Japanese' },
  { value: 'kn', label: 'Kannada' },
  { value: 'kk', label: 'Kazakh' },
  { value: 'ko', label: 'Korean' },
  { value: 'lv', label: 'Latvian' },
  { value: 'lt', label: 'Lithuanian' },
  { value: 'mk', label: 'Macedonian' },
  { value: 'ms', label: 'Malay' },
  { value: 'ml', label: 'Malayalam' },
  { value: 'mt', label: 'Maltese' },
  { value: 'mr', label: 'Marathi' },
  { value: 'mn', label: 'Mongolian' },
  { value: 'no', label: 'Norwegian (Bokmål)' },
  { value: 'ps', label: 'Pashto' },
  { value: 'pl', label: 'Polish' },
  { value: 'pt', label: 'Portuguese (Brazil)' },
  { value: 'pt-PT', label: 'Portuguese (Portugal)' },
  { value: 'pa', label: 'Punjabi' },
  { value: 'ro', label: 'Romanian' },
  { value: 'ru', label: 'Russian' },
  { value: 'sr', label: 'Serbian' },
  { value: 'si', label: 'Sinhala' },
  { value: 'sk', label: 'Slovak' },
  { value: 'sl', label: 'Slovenian' },
  { value: 'so', label: 'Somali' },
  { value: 'es', label: 'Spanish' },
  { value: 'es-MX', label: 'Spanish (Mexico)' },
  { value: 'sw', label: 'Swahili' },
  { value: 'sv', label: 'Swedish' },
  { value: 'ta', label: 'Tamil' },
  { value: 'te', label: 'Telugu' },
  { value: 'th', label: 'Thai' },
  { value: 'tr', label: 'Turkish' },
  { value: 'uk', label: 'Ukrainian' },
  { value: 'ur', label: 'Urdu' },
  { value: 'uz', label: 'Uzbek' },
  { value: 'vi', label: 'Vietnamese' },
  { value: 'cy', label: 'Welsh' },
] as const;
