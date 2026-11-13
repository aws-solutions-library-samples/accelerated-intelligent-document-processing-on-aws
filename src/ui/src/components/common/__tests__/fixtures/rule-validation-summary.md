<style>
table {
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
}

th, td {
    border: 1px solid #ddd;
    padding: 12px;
    text-align: left;
    vertical-align: top;
    word-wrap: break-word;
    overflow-wrap: break-word;
    white-space: normal;
}

th {
    background-color: #f1f1f1;
    font-weight: bold;
}

tr:nth-child(even) {
    background-color: #f9f9f9;
}

tr:hover {
    background-color: #f5f5f5;
}

/* Use colgroup to define column widths */
.rules-table {
    table-layout: fixed;
}

.rules-table col.rule-col {
    width: 18%;
}

.rules-table col.recommendation-col {
    width: 12%;
}

.rules-table col.reasoning-col {
    width: 60%;
}

.rules-table col.pages-col {
    width: 10%;
}
</style>

# Rule Validation Summary: lending_package.pdf

## Overall Statistics

| Metric | Value |
|--------|-------|
| Rules Evaluated (Pass / Fail / Info Not Found) | 2 (<span style="color: #16ab39; font-weight: bold;">1</span> / <span style="color: #d13212; font-weight: bold;">1</span> / 0) |
| Pass Percentage | 50.0% |

## Table of Contents

1. [Lending](#lending)

## 1. Lending <a id="lending"></a>

### Summary

| Metric | Value |
|--------|-------|
| Rules Evaluated (Pass / Fail / Info Not Found) | 2 (<span style="color: #16ab39; font-weight: bold;">1</span> / <span style="color: #d13212; font-weight: bold;">1</span> / 0) |
| Pass Percentage | 50.0% |

### Rules

<table style="width: 100%; border-collapse: collapse; table-layout: fixed;">
  <colgroup>
    <col style="width: 18%;">
    <col style="width: 12%;">
    <col style="width: 60%;">
    <col style="width: 10%;">
  </colgroup>
  <thead>
    <tr>
      <th style="border: 1px solid #ddd; padding: 12px; background-color: #f1f1f1; font-weight: bold; text-align: left; vertical-align: top; word-wrap: break-word;">Rule</th>
      <th style="border: 1px solid #ddd; padding: 12px; background-color: #f1f1f1; font-weight: bold; text-align: left; vertical-align: top; word-wrap: break-word;">Recommendation</th>
      <th style="border: 1px solid #ddd; padding: 12px; background-color: #f1f1f1; font-weight: bold; text-align: left; vertical-align: top; word-wrap: break-word;">Reasoning</th>
      <th style="border: 1px solid #ddd; padding: 12px; background-color: #f1f1f1; font-weight: bold; text-align: left; vertical-align: top; word-wrap: break-word;">Supporting Pages</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">Borrower name matches &lt;ID&gt;</td>
      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">✅ Pass</td>
      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">Names agree &amp; match</td>
      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">1, 3</td>
    </tr>
    <tr>
      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">Income documented</td>
      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">❌ Fail</td>
      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">No pay stub</td>
      <td style="border: 1px solid #ddd; padding: 12px; text-align: left; vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; white-space: normal;">2</td>
    </tr>
  </tbody>
</table>


[Back to Top](#table-of-contents)


---

*Report generated at: 2026-11-10T23:11:10.198441*
