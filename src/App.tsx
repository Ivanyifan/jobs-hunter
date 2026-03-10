/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { Folder, FileJson, FileCode, FileText, ChevronRight, ChevronDown, Play, Loader2 } from 'lucide-react';

const files = {
  'ai_job_copilot/data/projects_bank.json': `{
  "personal_info": {
    "first_name": "John",
    "last_name": "Doe",
    "email": "li62999772@gmail.com",
    "phone": "+1-555-0123",
    "linkedin": "linkedin.com/in/johndoe",
    "github": "github.com/johndoe",
    "portfolio": "johndoe.com",
    "education": [
      {
        "institution": "University of Illinois Urbana-Champaign",
        "degree": "Bachelor of Science in Computer Science",
        "graduation_date": "May 2026",
        "gpa": "3.9/4.0",
        "coursework": [
          "Data Structures",
          "Algorithms",
          "Machine Learning",
          "Database Systems"
        ]
      }
    ],
    "skills": {
      "languages": ["Python", "Haskell", "JavaScript", "TypeScript", "SQL"],
      "frameworks": ["React", "Node.js", "PyTorch", "Scikit-learn", "FastAPI"],
      "tools": ["Git", "Docker", "AWS", "Linux"]
    }
  },
  "projects": [
    {
      "id": "course_weaver",
      "name": "Course Weaver",
      "category": "Full-Stack Engineering",
      "date": "Aug 2024 - Present",
      "technologies": ["Python", "React", "TypeScript", "PostgreSQL", "Docker"],
      "summary": "A full-stack academic planning platform for university students.",
      "bullet_points": [
        "Architected a scalable microservices backend using Python and FastAPI to handle course data ingestion and user scheduling.",
        "Developed a responsive frontend using React and TypeScript, improving user engagement by 40%.",
        "Optimized database queries in PostgreSQL, reducing course search latency by 60%.",
        "Implemented CI/CD pipelines using GitHub Actions and Docker for seamless deployment."
      ],
      "metrics": {
        "users": "500+",
        "latency_reduction": "60%"
      }
    },
    {
      "id": "ml_research_odt",
      "name": "Optimal Decision Trees Research",
      "category": "Machine Learning Research",
      "date": "Jan 2024 - May 2024",
      "technologies": ["Python", "PyTorch", "Scikit-learn", "NumPy", "Pandas"],
      "summary": "Research on optimizing decision tree algorithms for ICML submission.",
      "bullet_points": [
        "Designed and implemented a novel Optimal Decision Tree algorithm in Python, improving classification accuracy by 15% on benchmark datasets.",
        "Conducted extensive experiments comparing the proposed method against state-of-the-art algorithms like XGBoost and Random Forest.",
        "Authored a comprehensive research paper detailing the methodology and experimental results, submitted to ICML.",
        "Optimized algorithmic complexity, reducing training time by 30% for large-scale datasets."
      ],
      "metrics": {
        "accuracy_improvement": "15%",
        "training_time_reduction": "30%"
      }
    }
  ],
  "experience": []
}`,
  'ai_job_copilot/modules/github_monitor.py': `import requests
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
        lines = readme_content.split('\\n')
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
                url_match = re.search(r'href="([^"]+)"', link_raw) or re.search(r'\\((https?://[^)]+)\\)', link_raw)
                url = url_match.group(1) if url_match else link_raw
                
                # Clean up markdown links or bold tags
                company_name = re.sub(r'\\[([^\\]]+)\\]\\(.*?\\)', r'\\1', company_raw)
                company_name = re.sub(r'\\*+', '', company_name).strip()
                
                role_name = re.sub(r'\\[([^\\]]+)\\]\\(.*?\\)', r'\\1', role_raw)
                role_name = re.sub(r'\\*+', '', role_name).strip()
                
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
`,
  'ai_job_copilot/main.py': `import os
from modules.github_monitor import GitHubJobMonitor

def main():
    print("Starting AI Job Application Copilot...")
    
    # Define paths
    base_dir = os.path.dirname(os.path.abspath(__file__))
    state_file = os.path.join(base_dir, "data", "monitor_state.json")
    
    # Initialize Module 1: GitHub Job Monitor
    monitor = GitHubJobMonitor(
        repo="speedyapply/2026-SWE-College-Jobs",
        state_file=state_file
    )
    
    # Run the monitor
    new_jobs = monitor.check_for_new_jobs()
    
    if new_jobs:
        print("\\n--- New Jobs Found ---")
        for job in new_jobs:
            print(f"Company: {job['company']}")
            print(f"Role: {job['role']}")
            print(f"URL: {job['url']}")
            print("-" * 20)
    else:
        print("No new jobs to process at this time.")

if __name__ == "__main__":
    main()
`,
  'ai_job_copilot/requirements.txt': `requests==2.31.0`
};

export default function App() {
  const [activeFile, setActiveFile] = useState<string>('ai_job_copilot/modules/github_monitor.py');
  const [viewMode, setViewMode] = useState<'code' | 'test'>('code');
  const [isTesting, setIsTesting] = useState(false);
  const [testResults, setTestResults] = useState<{company: string, role: string, url: string}[] | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const runLiveTest = async () => {
    setIsTesting(true);
    setTestError(null);
    setTestResults(null);
    
    try {
      // Simulate the exact logic from github_monitor.py
      const response = await fetch('https://api.github.com/repos/speedyapply/2026-SWE-College-Jobs/readme', {
        headers: { 'Accept': 'application/vnd.github.v3.raw' }
      });
      
      if (!response.ok) throw new Error(`GitHub API Error: ${response.status}`);
      
      const text = await response.text();
      const lines = text.split('\\n');
      let inTable = false;
      let headers: string[] = [];
      const jobs = [];
      
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('|') || !trimmed.endsWith('|')) {
          inTable = false;
          continue;
        }
        
        const parts = trimmed.split('|').slice(1, -1).map(p => p.trim());
        
        if (!inTable) {
          const lowerParts = parts.map(p => p.toLowerCase());
          if (lowerParts.some(p => p.includes('company')) && lowerParts.some(p => p.includes('role'))) {
            inTable = true;
            headers = lowerParts;
          }
          continue;
        }
        
        if (trimmed.includes('---')) continue;
        
        if (parts.length === headers.length) {
          const companyCol = headers.findIndex(h => h.includes('company'));
          const roleCol = headers.findIndex(h => h.includes('role'));
          const linkCol = headers.findIndex(h => h.includes('link') || h.includes('apply') || h.includes('application'));
          
          if (companyCol !== -1 && roleCol !== -1 && linkCol !== -1) {
            const companyRaw = parts[companyCol];
            const roleRaw = parts[roleCol];
            const linkRaw = parts[linkCol];
            
            const urlMatch = linkRaw.match(/href="([^"]+)"/) || linkRaw.match(/\((https?:\/\/[^)]+)\)/);
            const url = urlMatch ? urlMatch[1] : linkRaw;
            
            const company = companyRaw.replace(/\[([^\]]+)\]\(.*?\)/, '$1').replace(/\*+/g, '').trim();
            const role = roleRaw.replace(/\[([^\]]+)\]\(.*?\)/, '$1').replace(/\*+/g, '').trim();
            
            if (company && role && url.startsWith('http')) {
              jobs.push({ company, role, url });
            }
          }
        }
      }
      
      setTestResults(jobs);
    } catch (err: any) {
      setTestError(err.message || 'An error occurred during testing');
    } finally {
      setIsTesting(false);
    }
  };

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-300 font-sans flex flex-col">
      <header className="border-b border-zinc-800 bg-zinc-900 px-6 py-4 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-zinc-100">AI Job Application Copilot</h1>
          <p className="text-sm text-zinc-400">Phase 1: Core Data & GitHub Monitor</p>
        </div>
        <div className="flex items-center gap-4">
          <div className="flex bg-zinc-800 rounded-lg p-1">
            <button 
              onClick={() => setViewMode('code')}
              className={`px-3 py-1.5 text-sm rounded-md transition-colors ${viewMode === 'code' ? 'bg-zinc-700 text-white shadow-sm' : 'text-zinc-400 hover:text-zinc-200'}`}
            >
              Code View
            </button>
            <button 
              onClick={() => setViewMode('test')}
              className={`px-3 py-1.5 text-sm rounded-md transition-colors flex items-center gap-1.5 ${viewMode === 'test' ? 'bg-zinc-700 text-white shadow-sm' : 'text-zinc-400 hover:text-zinc-200'}`}
            >
              <Play size={14} />
              Live Test
            </button>
          </div>
          <span className="px-2 py-1 bg-emerald-500/10 text-emerald-400 text-xs rounded-md border border-emerald-500/20 font-mono">
            Python 3.10+
          </span>
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden">
        {/* Sidebar */}
        <div className="w-64 border-r border-zinc-800 bg-zinc-900/50 flex flex-col">
          <div className="p-4 text-xs font-semibold text-zinc-500 uppercase tracking-wider">
            Project Explorer
          </div>
          <div className="flex-1 overflow-y-auto px-2">
            
            {/* Folder: ai_job_copilot */}
            <div className="mb-1">
              <div className="flex items-center gap-1.5 px-2 py-1.5 text-sm text-zinc-300 font-medium">
                <ChevronDown size={14} className="text-zinc-500" />
                <Folder size={14} className="text-blue-400" />
                ai_job_copilot
              </div>
              
              <div className="ml-4 border-l border-zinc-800 pl-2">
                {/* Folder: data */}
                <div className="mb-1">
                  <div className="flex items-center gap-1.5 px-2 py-1.5 text-sm text-zinc-400">
                    <ChevronDown size={14} className="text-zinc-600" />
                    <Folder size={14} className="text-blue-400/70" />
                    data
                  </div>
                  <div className="ml-4 border-l border-zinc-800 pl-2">
                    <button 
                      onClick={() => { setActiveFile('ai_job_copilot/data/projects_bank.json'); setViewMode('code'); }}
                      className={`w-full flex items-center gap-2 px-2 py-1.5 text-sm rounded-md transition-colors ${activeFile === 'ai_job_copilot/data/projects_bank.json' && viewMode === 'code' ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200'}`}
                    >
                      <FileJson size={14} className="text-yellow-400" />
                      projects_bank.json
                    </button>
                    <div className="w-full flex items-center gap-2 px-2 py-1.5 text-sm rounded-md text-zinc-500">
                      <FileJson size={14} className="text-yellow-400/50" />
                      monitor_state.json
                    </div>
                  </div>
                </div>

                {/* Folder: modules */}
                <div className="mb-1">
                  <div className="flex items-center gap-1.5 px-2 py-1.5 text-sm text-zinc-400">
                    <ChevronDown size={14} className="text-zinc-600" />
                    <Folder size={14} className="text-blue-400/70" />
                    modules
                  </div>
                  <div className="ml-4 border-l border-zinc-800 pl-2">
                    <div className="w-full flex items-center gap-2 px-2 py-1.5 text-sm rounded-md text-zinc-500">
                      <FileCode size={14} className="text-blue-400/50" />
                      __init__.py
                    </div>
                    <button 
                      onClick={() => { setActiveFile('ai_job_copilot/modules/github_monitor.py'); setViewMode('code'); }}
                      className={`w-full flex items-center gap-2 px-2 py-1.5 text-sm rounded-md transition-colors ${activeFile === 'ai_job_copilot/modules/github_monitor.py' && viewMode === 'code' ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200'}`}
                    >
                      <FileCode size={14} className="text-blue-400" />
                      github_monitor.py
                    </button>
                  </div>
                </div>

                {/* Root files */}
                <button 
                  onClick={() => { setActiveFile('ai_job_copilot/main.py'); setViewMode('code'); }}
                  className={`w-full flex items-center gap-2 px-2 py-1.5 text-sm rounded-md transition-colors ${activeFile === 'ai_job_copilot/main.py' && viewMode === 'code' ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200'}`}
                >
                  <FileCode size={14} className="text-blue-400" />
                  main.py
                </button>
                <button 
                  onClick={() => { setActiveFile('ai_job_copilot/requirements.txt'); setViewMode('code'); }}
                  className={`w-full flex items-center gap-2 px-2 py-1.5 text-sm rounded-md transition-colors ${activeFile === 'ai_job_copilot/requirements.txt' && viewMode === 'code' ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200'}`}
                >
                  <FileText size={14} className="text-zinc-400" />
                  requirements.txt
                </button>
              </div>
            </div>

          </div>
        </div>

        {/* Main Content Area */}
        <div className="flex-1 flex flex-col bg-[#1e1e1e]">
          {viewMode === 'code' ? (
            <>
              <div className="flex items-center px-4 py-2 bg-[#2d2d2d] border-b border-[#1e1e1e]">
                <div className="flex items-center gap-2 text-sm text-zinc-300">
                  {activeFile.split('/').map((part, i, arr) => (
                    <React.Fragment key={i}>
                      <span>{part}</span>
                      {i < arr.length - 1 && <ChevronRight size={14} className="text-zinc-500" />}
                    </React.Fragment>
                  ))}
                </div>
              </div>
              <div className="flex-1 overflow-auto p-4">
                <pre className="font-mono text-sm leading-relaxed text-[#d4d4d4]">
                  <code>{files[activeFile as keyof typeof files]}</code>
                </pre>
              </div>
            </>
          ) : (
            <div className="flex-1 flex flex-col p-8 bg-zinc-950">
              <div className="max-w-3xl mx-auto w-full">
                <div className="mb-8">
                  <h2 className="text-2xl font-semibold text-white mb-2">Live Monitor Test</h2>
                  <p className="text-zinc-400">
                    This simulates the exact Python parsing logic written in <code>github_monitor.py</code>, 
                    but runs it directly in your browser using TypeScript to fetch and parse the live GitHub repository.
                  </p>
                </div>
                
                <button
                  onClick={runLiveTest}
                  disabled={isTesting}
                  className="mb-8 flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white px-6 py-3 rounded-lg font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {isTesting ? (
                    <><Loader2 size={18} className="animate-spin" /> Fetching & Parsing...</>
                  ) : (
                    <><Play size={18} /> Run Monitor Logic</>
                  )}
                </button>

                {testError && (
                  <div className="p-4 bg-red-500/10 border border-red-500/20 rounded-lg text-red-400 mb-8">
                    {testError}
                  </div>
                )}

                {testResults && (
                  <div className="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden">
                    <div className="px-6 py-4 border-b border-zinc-800 flex justify-between items-center bg-zinc-900/50">
                      <h3 className="font-medium text-zinc-200">Parsed Results</h3>
                      <span className="text-sm text-zinc-500">Found {testResults.length} jobs</span>
                    </div>
                    <div className="max-h-[500px] overflow-y-auto">
                      <table className="w-full text-left text-sm">
                        <thead className="bg-zinc-900/80 sticky top-0 backdrop-blur-sm">
                          <tr>
                            <th className="px-6 py-3 text-zinc-400 font-medium">Company</th>
                            <th className="px-6 py-3 text-zinc-400 font-medium">Role</th>
                            <th className="px-6 py-3 text-zinc-400 font-medium">URL</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-zinc-800/50">
                          {testResults.slice(0, 50).map((job, i) => (
                            <tr key={i} className="hover:bg-zinc-800/30 transition-colors">
                              <td className="px-6 py-3 text-zinc-300 font-medium">{job.company}</td>
                              <td className="px-6 py-3 text-zinc-400">{job.role}</td>
                              <td className="px-6 py-3">
                                <a href={job.url} target="_blank" rel="noreferrer" className="text-blue-400 hover:text-blue-300 truncate block max-w-[200px]">
                                  {job.url}
                                </a>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      {testResults.length > 50 && (
                        <div className="p-4 text-center text-zinc-500 text-sm border-t border-zinc-800/50">
                          Showing first 50 of {testResults.length} results...
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

