def analyze_log(log):
    if not log:
        return "No logs available"

    log_lower = log.lower()

    if "workflowscript" in log_lower and ("expecting '}'" in log_lower or "expecting }" in log_lower or "found 'eof'" in log_lower):
        return "Pipeline syntax error → Check for missing braces or malformed Jenkinsfile"

    if "multiple compilation errors" in log_lower and "workflowscript" in log_lower:
        return "Pipeline compilation error → Check Jenkinsfile syntax"

    if "illegal string body character" in log_lower and "workflowscript" in log_lower:
        return "Pipeline syntax error → Check Groovy quoting and interpolation"

    if "outofmemoryerror" in log_lower:
        return "JVM memory issue → Increase heap size"

    if "could not resolve dependencies" in log_lower:
        return "Dependency resolution issue → Check build configuration"

    if "permission denied" in log_lower:
        return "Permission issue → Verify credentials"

    if "connection refused" in log_lower:
        return "Service connectivity issue → Downstream service unavailable"

    if "timeout" in log_lower:
        return "Timeout detected → Check network or increase timeout"

    if "error" in log_lower:
        return "Generic error found → Inspect logs"

    return "Root cause not clearly identifiable from logs"
