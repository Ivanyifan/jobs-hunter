import os
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
        print("\n--- New Jobs Found ---")
        for job in new_jobs:
            print(f"Company: {job['company']}")
            print(f"Role: {job['role']}")
            print(f"URL: {job['url']}")
            print("-" * 20)
    else:
        print("No new jobs to process at this time.")

if __name__ == "__main__":
    main()
