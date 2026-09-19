# Read the timeout configuration

Read the file specified by input.json, resolving its path relative to the working directory.
Extract its integer timeout_s value and write the result according to output.schema.json.
Read only this file; do not search parent directories or other directories.
If the file does not exist, write the reason to refusal.json and call refuse_output.
