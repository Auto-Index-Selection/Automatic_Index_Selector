#!/usr/bin/env python3
import os
import sys
import time
import subprocess
import psycopg2
from dotenv import load_dotenv

def main():
    # 1. Load env and original DB name
    load_dotenv()
    original_db = os.getenv("DB_NAME")
    if not original_db:
        print("Error: DB_NAME not found in .env")
        sys.exit(1)
        
    backup_db = f"{original_db}_backup"
    
    print(f"============================================================")
    print(f"   Safe Test Runner (Target DB: {original_db})")
    print(f"============================================================\n")

    conn = psycopg2.connect(
        dbname="postgres",
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT")
    )
    conn.autocommit = True

    try:
        with conn.cursor() as cur:
            # Terminate connections to the original DB
            print(f"Terminating active connections to '{original_db}'...")
            cur.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{original_db}' AND pid <> pg_backend_pid();")
            
            # Drop backup if it somehow exists
            cur.execute(f"DROP DATABASE IF EXISTS {backup_db};")
            
            # Rename the original DB to the backup name
            print(f"Renaming '{original_db}' -> '{backup_db}'...")
            cur.execute(f"ALTER DATABASE {original_db} RENAME TO {backup_db};")
            
            # Create a clone with the original name for testing
            print(f"Cloning '{backup_db}' -> '{original_db}' for testing...")
            cur.execute(f"CREATE DATABASE {original_db} WITH TEMPLATE {backup_db};")
            
    except Exception as e:
        print(f"Failed to setup safe environment: {e}")
        conn.close()
        sys.exit(1)

    print("\nDatabase environment ready. Launching tests...\n")
    start_time = time.time()
    
    # 2. Run the tests. Since they use DB_NAME from .env, they will connect to the cloned DB.
    # The cloned DB is literally named the original name (e.g. tpcc), fulfilling the requirement.
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    
    test_proc = subprocess.Popen(
        [sys.executable, "-m", "tests.run_tests"],
        env=env
    )
    
    try:
        test_proc.wait()
    except KeyboardInterrupt:
        print("\nTests interrupted by user.")
        test_proc.terminate()
        test_proc.wait()

    # 3. Clean up and Restore
    print("\n============================================================")
    print(" Tests completed. Restoring original database...")
    print("============================================================\n")
    
    try:
        with conn.cursor() as cur:
            # Terminate connections to the test DB
            cur.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{original_db}' AND pid <> pg_backend_pid();")
            
            print(f"Dropping modified test DB '{original_db}'...")
            cur.execute(f"DROP DATABASE IF EXISTS {original_db};")
            
            # Terminate connections to backup DB (just in case)
            cur.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{backup_db}' AND pid <> pg_backend_pid();")
            
            print(f"Restoring '{backup_db}' -> '{original_db}'...")
            cur.execute(f"ALTER DATABASE {backup_db} RENAME TO {original_db};")
            
        print("\n✓ Original database restored perfectly to its pristine state.")
        
    except Exception as e:
        print(f"\nCRITICAL ERROR during restore: {e}")
        print(f"Your pristine database is currently named '{backup_db}'.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
