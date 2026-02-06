
import json

def clean_dump():
    print("Loading datadump.json...")
    with open('datadump.json', 'r') as f:
        data = json.load(f)
    
    cleaned_data = []
    removed_count = 0
    
    print("Cleaning data...")
    for obj in data:
        # Check CustomUser permissions
        if obj['model'] == 'users.customuser':
            if 'user_permissions' in obj['fields']:
                perms = obj['fields']['user_permissions']
                # Filter out permissions referencing 'userroutine'
                # The natural key structure is [codename, app_label, model]
                # We want to remove any where model is 'userroutine'
                valid_perms = []
                for p in perms:
                    # p is likely a list like ['view_userroutine', 'routine', 'userroutine']
                    if len(p) >= 3 and p[2] == 'userroutine':
                        print(f"Removing invalid permission: {p} from user {obj.get('pk', 'unknown')}")
                        removed_count += 1
                    else:
                        valid_perms.append(p)
                obj['fields']['user_permissions'] = valid_perms
        
        cleaned_data.append(obj)
    
    print(f"Removed {removed_count} invalid permission references.")
    
    print("Saving to datadump_clean.json...")
    with open('datadump_clean.json', 'w') as f:
        json.dump(cleaned_data, f)
    print("Done.")

if __name__ == '__main__':
    clean_dump()
