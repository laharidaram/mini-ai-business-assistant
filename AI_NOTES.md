# AI Development Notes

## 1. Project Overview

The Mini AI Business Intelligence Assistant was developed as part of an AI-assisted development challenge. The goal was to build a small application that allows users to ask questions about sales data in natural language and receive accurate, explainable answers supported by database queries.

AI assistance supported parts of the development process, but the central engineering goal was reliability. A convincing answer is not enough if its numbers are wrong. The application therefore separates question interpretation from database querying and result generation.

## 2. AI Tools and Development Tools Used

- **ChatGPT:** Used to help interpret the requirements, explore implementation approaches, review code and edge cases, troubleshoot errors, and draft project documentation.
- **Claude :** Include this only if you personally used the relevant tool during development, and briefly explain how.
- **Anthropic Claude API (application integration):** The application has an optional integration intended to interpret natural-language questions when an Anthropic API key is configured. This is separate from using Claude as a development assistant. Describe live API use as tested only if you actually tested it with a valid key.
- **Python and pytest:** Used to implement the application and run automated tests.
- **Browser and terminal:** Used to run the application, inspect responses, and debug issues.

AI-generated suggestions were treated as a starting point, not as proof that a feature worked correctly. Behaviour was checked against the requirements, the supplied CSV, and test cases.

## 3. How AI Assisted the Development Process

### Understanding the requirements and data

The first step was to understand the challenge requirements and inspect the sales dataset before designing the query logic. This was important because the CSV contains records that could lead to misleading totals if handled incorrectly.

Important data-quality cases include:

- `TXN0034` has a quantity of zero. Its revenue is known to be ₹0.
- `TXN0051` has a missing unit price. Its revenue cannot be calculated and must remain unknown.
- `TXN0033` is a returned transaction and is excluded from completed-sales revenue.

These cases informed the database rules, the explanations shown to users, and the tests.

### Designing the interpretation flow

AI assistance helped explore how a natural-language question could be mapped to a database query. The selected design uses supported intents and validated parameters rather than allowing an LLM to generate and execute arbitrary SQL.

For example, a question asking which product generated the lowest revenue is interpreted as a ranking request. The application then executes the corresponding query and builds the answer from the returned data.

This design is less flexible than unrestricted text-to-SQL, but it is easier to validate, test, and explain.

### Improving answers through testing

The application was tested with variations of questions, not just the examples shown in the interface. This helped investigate ranking direction, month comparisons, unsupported filters, and data-quality handling.

The development approach was iterative: reproduce a problem, identify the interpretation or query involved, make a targeted change, and add or update a test where appropriate.

## 4. Important Engineering Decisions

### Database-backed numbers

The LLM is not responsible for calculating revenue or inventing figures. It may help interpret the user's intent; the application retrieves numerical values through controlled query logic.

This makes numerical results reproducible and allows the query logic to be tested independently of the language model.

### Keep readable transactions and represent unknowns explicitly

The dataset contains 51 transactions. The application is designed to retain readable transactions, including records that cannot contribute a known revenue value.

The important cases are:

- `zero_quantity`: revenue is known to be zero.
- `unknown_price`: revenue cannot be calculated because the unit price is missing.
- Returned transaction: retained for data-quality and return-related reporting but excluded from completed-sales revenue.

The missing-price transaction is not assigned revenue of ₹0. Its revenue remains unknown and is excluded from revenue totals. **Unknown is not the same as zero.**

### Returned orders are not automatically financial losses

The dataset records the sales value associated with a returned order, but does not provide all the information needed to calculate actual financial loss. The application therefore reports the recorded value with a limitation rather than presenting it as a confirmed loss.

### Fail safely when the question is unsupported

A question about an unsupported period, filter, or metric should not quietly return an all-time total or an unrelated result. The application should explain what it cannot determine and, where possible, suggest a supported alternative.

### Keep AI integration optional

The rule-based interpreter allows the application to run without a third-party API key. Optional Claude integration can help with less predictable wording, but both paths should use the same validated query layer.

This keeps the application usable without API access and prevents a model response from bypassing query controls.

## 5. Testing and Debugging Approach

Testing should cover both expected behaviour and cases where the application might return a plausible but incorrect answer.

| Area | What to verify |
|---|---|
| Revenue calculations | Totals are computed from eligible completed transactions |
| Data quality | All 51 supplied transactions are accounted for according to the loading rules |
| Missing price | `TXN0051` has unknown revenue and is not treated as ₹0 |
| Zero quantity | `TXN0034` has revenue ₹0 and is not classified as a missing-price transaction |
| Returned orders | `TXN0033` is reported separately from completed-sales revenue |
| Ranking | Highest, lowest, and second-highest questions use the intended direction and rank |
| Month comparisons | The direction and percentage of change match the selected months |
| Unsupported questions | The application explains the limitation rather than silently returning an unrelated answer |
| Security | User input cannot execute arbitrary or destructive SQL |
| API behaviour | Invalid requests and unsupported questions are handled appropriately |

Where possible, expected values should be calculated independently from the original CSV instead of using the application's own query functions as the test oracle. This reduces the risk of a test repeating the same bug as the implementation.

Run the full test suite from the project root using:

```bash
python -m pytest -v
```

Also check the web interface and test rule-based mode. Test live LLM mode only if API access is available. Do not report live API calls or tests as verified unless they actually ran successfully.

## 6. Challenges and Lessons Learned

### Natural language is ambiguous

Different questions can use similar words while asking for different things. For example, “Which region generated the highest revenue?” and “Which region performed worst?” require opposite ranking directions.

**Lesson:** Interpretation must identify the intended metric and direction explicitly. Tests should cover different phrasings of the same intent.

### Missing data requires deliberate handling

A missing unit price can lead to incorrect totals if the record is dropped or its revenue is silently set to zero.

**Lesson:** Data-quality rules should be explicit in the loading logic, query logic, answer text, and tests. A consistent definition of revenue is just as important as the SQL calculation itself.

### A successful response is not necessarily a correct response

An application can return a well-formatted table and chart while answering the wrong question. Unsupported date filters and ambiguous comparisons are especially risky because the result may still look reasonable.

**Lesson:** Validate the meaning of the question before executing a query, and make unsupported cases visible to the user.

### AI-generated code still needs verification

AI tools can accelerate implementation and suggest useful edge cases, but they can also introduce incorrect assumptions, incomplete error handling, or inconsistent behaviour across modules.

**Lesson:** Use AI to speed up development, then rely on independent calculations, targeted tests, and manual checks to establish whether the implementation is correct.

## 7. Responsible AI Use

The application is designed so that the language model does not have unrestricted access to the database. Questions are mapped to supported intents, and the application validates the interpretation before running controlled queries.

This approach reduces the risks of unsafe SQL execution and fabricated numerical answers. It also makes the system easier to debug because the interpretation, query, and returned data can be inspected separately.

The system still has limitations: a language model may misinterpret a loosely phrased question, API access can fail, and the rule-based fallback cannot understand every possible wording. These limitations should be tested and disclosed rather than hidden.

## 8. What I Would Improve Next

With another day of development, I would focus on:

1. Expanding supported question types while preserving validation.
2. Building a labelled evaluation set to measure interpretation accuracy across different phrasings.
3. Adding more date-period and product/region filters with explicit tests.
4. Improving error messages so users know how to rephrase unsupported questions.
5. Automating the test suite in continuous integration.
6. Adding CSV upload and schema validation for datasets beyond the supplied example.

## 9. Final Reflection

The biggest lesson from this project is that adding AI to an application is not just about making the interface conversational. The underlying data, query logic, validation, and explanations still need to be dependable.

AI assistance helped explore implementation choices and identify edge cases, but testing and careful handling of the data were essential to turning those ideas into a useful application. The result is a small business intelligence tool intended to make sales analysis more accessible while keeping its calculations transparent and verifiable.
