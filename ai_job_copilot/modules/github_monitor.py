import requests
import json
import os
import re
from typing import List, Dict, Any

class GitHubJobMonitor:
    """
    Module 1: GitHub Job Monitor
    Responsible for fetching updates from speedyapply/2026-SWE-College-Jobs
    and parsing the README.md to extract Company, Role, and URL.
    """
    def __init__(self, repo: str, state_file: str):
        self.repo = repo
        self.api_url = f"https://api.github.com/repos/{repo}/readme"
        self.state_file = state_file
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        """Loads the local state to avoid duplicate processing."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                pass
        return {"last_commit_sha": "", "seen_urls": []}

    def _save_state(self):
        """Saves the local state."""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, indent=2)

    def fetch_readme(self) -> str:
        """Fetches the raw README.md content from the GitHub API."""
        headers = {"Accept": "application/vnd.github.v3.raw"}
        response = requests.get(self.api_url, headers=headers)
        response.raise_for_status()
        return response.text

    def fetch_latest_commit_sha(self) -> str:
        """Fetches the latest commit SHA for the README.md file."""
        commits_url = f"https://api.github.com/repos/{self.repo}/commits?path=README.md&page=1&per_page=1"
        response = requests.get(commits_url)
        response.raise_for_status()
        commits = response.json()
        if commits:
            return commits[0]['sha']
        return ""

    def parse_jobs_from_readme(self, readme_content: str) -> List[Dict[str, str]]:
        """
        Parses Markdown tables to extract Company, Role, and URL.
        """
        jobs = []
        lines = readme_content.split('\n')
        in_table = False
        headers = []
        
        for line in lines:
            line = line.strip()
            if not line.startswith('|') or not line.endswith('|'):
                in_table = False
                continue
                
            parts = [p.strip() for p in line.split('|')[1:-1]]
            
            if not in_table:
                # Check if this row looks like a header
                lower_parts = [p.lower() for p in parts]
                if any('company' in p for p in lower_parts) and any('role' in p for p in lower_parts):
                    in_table = True
                    headers = lower_parts
                continue
                
            if '---' in line:
                continue
                
            if len(parts) == len(headers):
                job_data = dict(zip(headers, parts))
                
                # Find the relevant columns based on partial matches
                company_col = next((h for h in headers if 'company' in h), None)
                role_col = next((h for h in headers if 'role' in h), None)
                link_col = next((h for h in headers if 'link' in h or 'application' in h or 'apply' in h), None)
                
                if not (company_col and role_col and link_col):
                    continue
                    
                company_raw = job_data[company_col]
                role_raw = job_data[role_col]
                link_raw = job_data[link_col]
                
                # Extract URL
                url_match = re.search(r'href="([^"]+)"', link_raw) or re.search(r'\((https?://[^)]+)\)', link_raw)
                url = url_match.group(1) if url_match else link_raw
                
                # Clean up markdown links or bold tags
                company_name = re.sub(r'\[([^\]]+)\]\(.*?\)', r'\1', company_raw)
                company_name = re.sub(r'\*+', '', company_name).strip()
                
                role_name = re.sub(r'\[([^\]]+)\]\(.*?\)', r'\1', role_raw)
                role_name = re.sub(r'\*+', '', role_name).strip()
                
                # Basic validation
                if company_name and role_name and url.startswith('http'):
                    jobs.append({
                        "company": company_name,
                        "role": role_name,
                        "url": url
                    })
                    
        return jobs

    def check_for_new_jobs(self) -> List[Dict[str, str]]:
        """
        Main entry point to check for new jobs.
        Returns a list of newly found jobs.
        """
        print(f"Checking for updates in {self.repo}...")
        try:
            latest_sha = self.fetch_latest_commit_sha()
            if latest_sha == self.state.get("last_commit_sha"):
                print("No new commits to README.md. Everything is up to date.")
                return []
                
            readme_content = self.fetch_readme()
            all_jobs = self.parse_jobs_from_readme(readme_content)
            
            new_jobs = []
            seen_urls = set(self.state.get("seen_urls", []))
            
            for job in all_jobs:
                if job['url'] not in seen_urls:
                    new_jobs.append(job)
                    seen_urls.add(job['url'])
            
            # Update state
            self.state["last_commit_sha"] = latest_sha
            self.state["seen_urls"] = list(seen_urls)
            self._save_state()
            
            print(f"Found {len(new_jobs)} new jobs.")
            return new_jobs
            
        except Exception as e:
            print(f"Error checking for jobs: {e}")
            return []

if __name__ == "__main__":
    # Example usage
    monitor = GitHubJobMonitor(
        repo="speedyapply/2026-SWE-College-Jobs",
        state_file="../data/monitor_state.json"
    )
    new_jobs = monitor.check_for_new_jobs()
    for job in new_jobs:
        print(f"New Job: {job['company']} - {job['role']} ({job['url']})")
