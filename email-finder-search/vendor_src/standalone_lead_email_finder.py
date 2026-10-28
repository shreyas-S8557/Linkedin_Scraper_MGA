#!/usr/bin/env python3
"""
standalone_lead_email_finder.py

Finds potential email addresses associated with a company domain and an optional CEO name
using SearXNG web search and regex extraction.
"""

import json
from pathlib import Path
import re
import time
import sys
import os
import argparse
import logging
import traceback
from urllib.parse import urlparse, quote_plus
from typing import Dict, List, Set, Optional

# --- Library Imports ---
import requests

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s [%(name)s] %(message)s', stream=sys.stdout)
logger = logging.getLogger("lead_email_finder")

# --- Configuration Constants (from searx_email_collector) ---
BLACKLISTED_DOMAINS: Set[str] = {
    "email-format.com", "rocketreach.co", "hunter.io", "clearbit.com", "apollo.io",
    "emailhippo.com", "mailcheck.ai", "verify-email.org", "email-checker.net",
    "findemails.com", "findthat.email", "skymem.info", "anymail.com", "snov.io",
    "thatsthem.com", "emailfinder.io", "aol.com", "gmail.com", "googlemail.com",
    "hotmail.com", "msn.com", "live.com", "yahoo.com", "outlook.com",
    "gmx.com", "mail.com", "example.com", "wix.com", "squarespace.com", "godaddy.com",
    "protobuf.com", "zoho.com", "yandex.com", "protonmail.com", "github.com",
    "icloud.com", "privaterelay.appleid.com", "linkedin.com", "facebook.com", "twitter.com",
    "instagram.com", "support.com", "service.com", "info.com", "mail.com",
    # Add generic TLDs often used for spam/temp if needed
    ".info", ".xyz", ".online", ".club", ".site", ".live" # Check if this causes issues
}

# Regular expression to find email addresses
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# Number of search result pages to check per query
PAGES_PER_QUERY = 2 # Keep this relatively low for a CLI tool
# --- End Configuration ---


# --- Integrated URL Sanitization Logic ---
def sanitize_url(url: str) -> Optional[str]:
    """
    Clean and sanitize URLs, extracting just the domain without 'www.'.
    Returns None if invalid/error.
    """
    if not isinstance(url, str) or not url.strip():
        logger.debug("Invalid URL input for sanitization: empty or not a string.")
        return None
    original_url = url # Keep original for logging if needed

    if not url.startswith(('http://', 'https://', '//')):
        url = 'https://' + url

    try:
        parsed_url = urlparse(url)
        domain = parsed_url.netloc

        if not domain: # Handle cases like 'https://'
            return None

        if domain.lower().startswith('www.'):
            domain = domain[4:]

        domain = re.sub(r':\d+$', '', domain) # Remove port

        # Basic check for a valid domain structure (must contain at least one dot)
        if '.' not in domain or len(domain.split('.')[-1]) < 2:
            logger.debug(f"URL '{original_url}' resulted in invalid domain structure '{domain}'")
            return None

        return domain.lower()
    except Exception as e:
        logger.error(f"Error processing URL '{original_url}' for sanitization: {e}")
        return None

# --- Integrated SearXNG Client Logic ---
class SearXNGClient:
    """Handles communication with a SearXNG instance."""
    def __init__(self, base_url):
        if not base_url or not base_url.startswith(('http://', 'https://')):
             raise ValueError("Invalid SearXNG base URL provided.")
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        # Use a common user agent
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        logger.debug(f"SearXNGClient initialized with base URL: {self.base_url}")

    def search(self, query, max_pages=2, timeout=15):
        """Search SearXNG and return simplified results."""
        all_results_text = "" # Collect text from all results
        page = 1
        total_results_count = 0
        logger.debug(f"Searching SearXNG for query: '{query}' (Max pages: {max_pages})")

        while page <= max_pages:
            encoded_query = quote_plus(query)
            url = f"{self.base_url}/search?q={encoded_query}&format=json&pageno={page}"
            logger.debug(f"Fetching SearXNG page {page}: {url}")

            try:
                response = self.session.get(url, timeout=timeout)
                response.raise_for_status() # Raise HTTPError for bad responses
                data = response.json()

                page_results = data.get('results', [])
                if page_results:
                    count = len(page_results)
                    total_results_count += count
                    logger.debug(f"Got {count} results from page {page}.")
                    # Extract relevant text from title and content for email searching
                    for result in page_results:
                        title = result.get('title', '') or ''
                        content = result.get('content', '') or ''
                        all_results_text += f"{title}\n{content}\n\n" # Add separators
                    page += 1
                    time.sleep(0.3) # Small polite delay between pages
                else:
                    logger.debug(f"No more results found on page {page} or subsequent pages.")
                    break # Stop if a page returns no results

            except requests.exceptions.Timeout:
                logger.error(f"SearXNG request timed out for page {page} on query '{query}'.")
                break # Stop fetching for this query on timeout
            except requests.exceptions.RequestException as e:
                logger.error(f"SearXNG request error for page {page} on query '{query}': {e}")
                break # Stop fetching for this query on other request errors
            except json.JSONDecodeError:
                logger.error(f"SearXNG JSON decode error for page {page} on query '{query}'. Invalid response.")
                break # Stop fetching, response is unusable
            except Exception as e:
                logger.error(f"Unexpected error during SearXNG search for page {page}, query '{query}': {e}")
                logger.debug(traceback.format_exc())
                break # Stop on unexpected errors

        logger.debug(f"Finished SearXNG query '{query}'. Total results considered: {total_results_count}. Total text length: {len(all_results_text)}.")
        return all_results_text # Return the aggregated text content

# Modified search_web to directly return text
def search_web_standalone_text(query: str, searx_base_url: str, pages: int = 2) -> str:
    """
    Standalone web search function that returns aggregated text content.
    Removes retry/restart logic for simplicity in this tool.
    """
    if not searx_base_url:
        logger.error("No SearXNG base URL provided.")
        return "" # Return empty string on config error

    try:
        client = SearXNGClient(searx_base_url)
        aggregated_text = client.search(query, max_pages=pages)
        return aggregated_text if aggregated_text is not None else ""
    except ValueError as e:
        logger.error(f"Failed to initialize SearXNGClient: {e}")
        return ""
    except Exception as e:
        logger.error(f"Unexpected error during search_web_standalone_text: {e}")
        logger.debug(traceback.format_exc())
        return ""

# --- Email Extraction and Filtering Logic (from searx_email_collector) ---
def extract_emails_from_text(text: str) -> Set[str]:
    """Extracts unique potential email addresses from text."""
    if not text:
        return set()
    # Use finditer for potentially better memory usage on huge text, though findall is fine here
    found_emails_set = {email.lower() for email in EMAIL_REGEX.findall(text)}
    logger.debug(f"Extracted {len(found_emails_set)} unique potential emails.")
    return found_emails_set

def filter_blacklisted_emails(emails: Set[str], target_domain: Optional[str] = None) -> List[str]:
    """Filters emails against blacklist and optionally ensures they match the target domain."""
    valid_emails = []
    logger.debug(f"Filtering {len(emails)} emails against blacklist and target domain '{target_domain}'.")

    # Normalize target domain if provided
    normalized_target_domain = target_domain.lower() if target_domain else None

    for email in emails:
        try:
            _, domain_part = email.split('@', 1) # domain_part is already lowercase

            # Check 1: Blacklisted domain
            if domain_part in BLACKLISTED_DOMAINS:
                logger.debug(f"Ignoring blacklisted domain: {email}")
                continue
            # Check 2: Generic TLDs sometimes used for spam/junk (optional stricter check)
            # Note: This might exclude valid emails on newer TLDs. Use with caution.
            # if any(domain_part.endswith(tld) for tld in ['.info', '.xyz', '.online', '.club', '.site', '.live']):
            #     logger.debug(f"Ignoring potentially generic TLD: {email}")
            #     continue

            # Check 3: If target_domain is provided, ensure email matches
            if normalized_target_domain and domain_part != normalized_target_domain:
                logger.debug(f"Ignoring email from non-target domain: {email} (Expected: {normalized_target_domain})")
                continue

            # If all checks pass
            valid_emails.append(email)
            logger.debug(f"Keeping valid email: {email}")

        except ValueError:
            logger.warning(f"Skipping potentially malformed email candidate: {email}")
            continue
    logger.info(f"Filtering complete. Kept {len(valid_emails)} emails.")
    return sorted(list(set(valid_emails))) # Return sorted unique list

# --- Main Email Finding Logic ---
def find_emails_logic(company_input: str, ceo_name: Optional[str], searx_url: str) -> Dict:
    """
    Core logic to find emails for a company/domain and optional CEO name.

    Args:
        company_input (str): Company name or domain/URL.
        ceo_name (Optional[str]): CEO's full name (can be None).
        searx_url (str): Base URL of the SearXNG instance.

    Returns:
        Dict: Result dictionary containing 'query_details', 'found_emails', or 'error'.
    """
    result_dict = {"query_details": {}, "found_emails": [], "error": None}
    logger.info(f"Starting email search for input: '{company_input}'" + (f" (CEO: '{ceo_name}')" if ceo_name else ""))

    # 1. Determine Domain and Search Name
    target_domain = None
    search_name = company_input # Default to using input as name

    if '.' in company_input: # Basic check if it could be a domain/URL
        sanitized = sanitize_url(company_input)
        if sanitized:
            target_domain = sanitized
            # Use a potentially cleaner name from the domain for the query
            # search_name = target_domain.split('.')[0] # e.g., 'google' from 'google.com'
            # Keep original company input if it wasn't just a domain? Or prioritize domain?
            # Let's prioritize the original input if it wasn't *just* a domain
            if company_input.lower() != target_domain and not company_input.startswith(('http','//')):
                 search_name = company_input # Keep original name if provided alongside domain-like string
            else:
                 search_name = target_domain.split('.')[0] if '.' in target_domain else target_domain # Use domain part as name if only domain given

            logger.info(f"Input identified as domain/URL. Sanitized Domain: '{target_domain}'. Using search name: '{search_name}'.")
        else:
            logger.warning(f"Input looked like domain but failed sanitization: '{company_input}'. Treating as company name.")
            search_name = company_input
    else:
        logger.info(f"Input treated as company name: '{company_input}'")
        search_name = company_input
        # Optional: Try to *guess* domain from name - can be unreliable
        # guessed_domain = search_name.lower().replace(' ','').replace('.','') + ".com"
        # target_domain = sanitize_url(guessed_domain)
        # if target_domain: logger.info(f"Guessed domain: {target_domain}")

    result_dict["query_details"]["search_name"] = search_name
    result_dict["query_details"]["target_domain"] = target_domain

    # 2. Define Search Query Patterns (Placeholders: {name}, {domain}, {search_name})
    # Use different queries based on whether we have CEO name and a confirmed domain
    query_templates = []
    if ceo_name and target_domain:
        query_templates.extend([
            '"{name}" "{domain}" email',
            '"{name}" email address {domain}',
            'contact "{name}" {domain}',
            '"{domain}" email', # Broader fallback
            'contact {domain}' # Broader fallback
        ])
    elif ceo_name and not target_domain: # Have CEO but no confirmed domain
         query_templates.extend([
            '"{name}" "{search_name}" email',
            '"{name}" email address "{search_name}" company',
            'contact "{name}" "{search_name}"',
            '"{search_name}" email', # Broader fallback
         ])
    elif target_domain and not ceo_name: # Have domain but no CEO
         query_templates.extend([
             '"{domain}" email address',
             'contact OR support email "{domain}"',
             '"{domain}" company contact',
             'site:{domain} email OR contact' # Site-specific search
         ])
    else: # Only have search_name (company name)
         query_templates.extend([
             '"{search_name}" contact email',
             '"{search_name}" customer service email',
             '"{search_name}" company email address',
             'email address for "{search_name}"'
         ])

    # Add generic permutation attempts if CEO name is provided
    if ceo_name:
         name_parts = ceo_name.lower().split()
         if len(name_parts) >= 2:
              fname = name_parts[0]
              lname = name_parts[-1]
              # Add patterns like f.last, first.last etc. if domain known
              if target_domain:
                  query_templates.extend([
                      f'"{fname[0]}{lname}@{target_domain}"',
                      f'"{fname}.{lname}@{target_domain}"',
                      f'"{fname}{lname}@{target_domain}"',
                      f'"{lname}.{fname}@{target_domain}"', # Less common but possible
                      f'"{fname}@{target_domain}"', # First name only
                  ])

    all_found_emails_set: Set[str] = set()

    logger.info(f"Running {len(query_templates)} search queries...")
    for i, template in enumerate(query_templates):
        # Format the query safely
        query = template.format(name=ceo_name or "", domain=target_domain or "", search_name=search_name or "")
        query = query.replace('"" ', '').replace(' ""', '') # Clean up empty placeholders
        logger.info(f"--- Query {i+1}/{len(query_templates)}: {query} ---")

        # Perform search using standalone function that returns aggregated text
        aggregated_text = search_web_standalone_text(query, searx_url, pages=PAGES_PER_QUERY)

        if aggregated_text:
            emails_from_query = extract_emails_from_text(aggregated_text)
            if emails_from_query:
                logger.info(f"Found {len(emails_from_query)} potential emails from query {i+1}.")
                all_found_emails_set.update(emails_from_query)
            else:
                logger.debug(f"No potential emails extracted from text for query {i+1}.")
        else:
            logger.warning(f"No text content retrieved for query {i+1}.")

    logger.info(f"Search phase complete. Total unique potential emails found: {len(all_found_emails_set)}")

    # 3. Filter Emails
    if not all_found_emails_set:
        logger.info("No potential emails found to filter.")
        result_dict["found_emails"] = []
    else:
        # Filter against blacklist AND ensure emails match the target domain if one was identified
        final_emails = filter_blacklisted_emails(all_found_emails_set, target_domain)
        result_dict["found_emails"] = final_emails

    return result_dict

# --- Main Execution Block ---
def main():
    parser = argparse.ArgumentParser(
        description="Standalone Lead Email Finder: Finds potential emails for a company/domain using SearXNG.",
        epilog="Example: python standalone_lead_email_finder.py example.com --searx-url http://localhost:8080 --ceo-name \"Jane Doe\" -f json -o emails.json"
    )
    parser.add_argument("company_input", help="The company name or domain/URL (e.g., 'Example Corp', 'example.com').")
    parser.add_argument("-s", "--searx-url", required=True, help="Base URL of the SearXNG instance.")
    parser.add_argument("-c", "--ceo-name", help="Optional: Full name of the CEO or contact person to prioritize.", default=None)
    parser.add_argument("-o", "--output-file", help="Path to save the output JSON file.", default=None)
    parser.add_argument("-f", "--output-format", choices=['json', 'txt'], default='json', help="Output format. 'json' outputs full details, 'txt' outputs only emails (one per line). Default: json")
    parser.add_argument("-l", "--log-level", default="INFO", choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], help="Set logging level.")

    args = parser.parse_args()

    # Configure Logging Level
    log_level_map = {'DEBUG': logging.DEBUG, 'INFO': logging.INFO, 'WARNING': logging.WARNING, 'ERROR': logging.ERROR, 'CRITICAL': logging.CRITICAL}
    log_level = log_level_map.get(args.log_level.upper(), logging.INFO)
    logger.setLevel(log_level)
    logging.getLogger("requests").setLevel(logging.WARNING) # Quieten requests library
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    logger.info("="*30)
    logger.info(" Starting Lead Email Finder ".center(30,"="))
    logger.info("="*30)
    start_time = time.time()

    result = find_emails_logic(
        company_input=args.company_input,
        ceo_name=args.ceo_name,
        searx_url=args.searx_url
    )

    end_time = time.time()
    logger.info(f"Email search finished in {end_time - start_time:.2f} seconds.")
    logger.info(f"Found {len(result.get('found_emails',[]))} valid email(s).")

    # --- Handle Output ---
    output_content = ""
    if args.output_format == 'json':
        # Output the full result dictionary
        output_content = json.dumps(result, indent=2, ensure_ascii=False)
    else: # txt format
        if result.get("found_emails"):
            output_content = "\n".join(result["found_emails"])
        elif result.get("error"):
             output_content = f"Error: {result['error']}"
        else:
             output_content = "# No emails found" # Indicate no results cleanly

    if args.output_file:
        try:
            output_path = Path(args.output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(output_content)
            logger.info(f"Output successfully saved to: {args.output_file}")
        except Exception as e:
            logger.error(f"Failed to write output to file '{args.output_file}': {e}")
            print("\n--- Output (Error writing to file, fallback to Console) ---")
            print(output_content)
            print("--- End Output ---")
    else:
        # Print to console
        print("\n--- Output ---")
        print(output_content)
        print("--- End Output ---")

    if result.get("error"):
        sys.exit(1) # Exit with error code if an error occurred
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()