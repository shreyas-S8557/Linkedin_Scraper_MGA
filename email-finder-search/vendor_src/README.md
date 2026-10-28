# Standalone Lead Email Finder

A Python script to find potential email addresses associated with a company domain and optionally a specific contact name (like a CEO). It uses SearXNG for web searching and applies regex and filtering rules.

## Features

* Finds potential email addresses from public web data.
* Accepts company name or domain/URL as primary input.
* Optionally accepts a CEO/contact name to refine search queries.
* Uses a SearXNG instance for web searches.
* Extracts emails from search result text content.
* Filters results against a blacklist of common/generic email providers (e.g., gmail.com, hunter.io).
* Optionally filters emails to match only the target company domain.
* Handles domain sanitization.
* Outputs results as a JSON list or a simple text list (one email per line).
* Standalone CLI Tool.
* Configurable logging level.

## Prerequisites

1. **Python:** Python 3.7+ recommended.
2. **Pip:** Python package installer.
3. **SearXNG Instance:** Access to a running SearXNG instance and its base URL.
4. **Libraries:** `requests` (Install via `requirements.txt`).

## Installation

1. **Clone the repository or download the script:**
2. ```bash
   git clone https://github.com/Aboodseada1/Email-Finder
   cd https://github.com/Aboodseada1/Email-Finder
   ```
3. Or simply download `standalone_lead_email_finder.py` and `requirements.txt`.
4. **(Recommended)** Create and activate a Python virtual environment:
5. ```bash
   python -m venv venv
   source venv/bin/activate # On Windows use `venv\Scripts\activate`
   ```
6. **Install dependencies:**
7. ```bash
   pip install -r requirements.txt
   ```

## Usage

Run from your terminal:

```bash
python standalone_lead_email_finder.py <company_input> -s <searx_instance_url> [options]
```

**Arguments:**

* `company_input`: (Required) The company name (e.g., `"Example Corp"`) or domain/URL (e.g., `example.com`).
* `-s`, `--searx-url`: (Required) The base URL of your SearXNG instance.
* `-c`, `--ceo-name`: (Optional) Full name of the CEO or contact person to focus the search.
* `-o`, `--output-file`: (Optional) Path to save the output file. If omitted, output goes to console.
* `-f`, `--output-format`: (Optional) Output format: `json` (full details) or `txt` (emails only, one per line). Default: `json`.
* `-l`, `--log-level`: (Optional) Set logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Default: `INFO`.

## Examples

*(Replace placeholders)*

```bash
# Find emails for a domain, output JSON to console
python standalone_lead_email_finder.py example.com -s http://localhost:8080

# Find emails for a company name and CEO, output JSON to file
python standalone_lead_email_finder.py "Acme Corp" -c "John Smith" -s https://searx.example.org -o acme_emails.json

# Find emails for a domain, output only emails to text file
python standalone_lead_email_finder.py microsoft.com -s http://localhost:8080 -f txt -o ms_emails.txt

# Run with debug logging
python standalone_lead_email_finder.py google.com -s http://localhost:8080 -l DEBUG
```

## Output Format

**JSON Format (`-f json`, default):** Outputs a JSON object:

```json
{
  "query_details": {
    "search_name": "used search name",
    "target_domain": "identified target domain or null"
  },
  "found_emails": [
    "email1@example.com",
    "email2@example.com"
  ],
  "error": "Error message if any, otherwise null"
}
```

**TXT Format (`-f txt`):** Outputs only the found, valid email addresses, each on a new line. If no emails are found, it outputs `# No emails found`. If an error occurred, it outputs `Error: <error message>`.

## How It Works

1. **Input Processing:** Identifies if input is a domain/URL or company name; sanitizes domains.
2. **Query Generation:** Creates targeted SearXNG queries based on available info (domain, name, CEO name). Includes specific patterns like `"ceo name" "domain" email` and broader searches.
3. **SearXNG Search:** Fetches search results from the provided SearXNG instance for each query.
4. **Extraction:** Uses regex (`EMAIL_REGEX`) to find potential email addresses within the aggregated text content of search results.
5. **Filtering:** Removes emails from common/generic providers (`BLACKLISTED_DOMAINS`) and, importantly, **filters to keep only emails matching the identified `target_domain`** (if one was found).
6. **Output:** Returns the unique, filtered email addresses in the chosen format (JSON or TXT).

## Limitations

* Relies entirely on data available through SearXNG search results.
* Accuracy depends on search engine indexing and website content.
* The `EMAIL_REGEX` is broad and might occasionally match non-email strings (though unlikely with the `@` and `.` requirements).
* Filtering is based on the `BLACKLISTED_DOMAINS` list and matching the target domain; legitimate emails on other domains might be missed if filtering strictly by target domain.
* Does not verify if the found email addresses actually exist or are deliverable.

## Dependencies

* `requests`

## Contributing

Improvements (e.g., better query generation, refined filtering) are welcome! Please open an issue or submit a pull request on [GitHub](https://github.com/Aboodseada1?tab=repositories).

## Support Me

If you find this tool useful, consider supporting its development via [PayPal](http://paypal.me/aboodseada1999). Thank you!

## License

This project is licensed under the MIT License.

```
MIT License

Copyright (c) 2025 Abood

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```