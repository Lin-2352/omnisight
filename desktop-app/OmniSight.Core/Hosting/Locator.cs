namespace OmniSight.Core.Hosting;

/// <summary>Finds the repository (for desktop-client\main.py) and the Python to run it with.</summary>
public static class Locator
{
    public const string RootVariable = "OMNISIGHT_ROOT";
    public const string PythonVariable = "OMNISIGHT_PYTHON";

    /// <summary>
    /// Extra arguments for the Python client when the app starts it. Deliberately has no <c>--backend</c>: that flag beats the
    /// engine the user saved, so passing it would quietly turn "This PC" (screenshots stay local) back into Auto.
    /// </summary>
    public static readonly IReadOnlyList<string> AppArguments = [];

    public static string? FindRepoRoot(string startDirectory, Func<string, string?>? env = null)
    {
        env ??= Environment.GetEnvironmentVariable;
        var configured = env(RootVariable);
        if (!string.IsNullOrWhiteSpace(configured))
        {
            return HasMain(configured) ? Path.GetFullPath(configured) : null;
        }

        var directory = new DirectoryInfo(startDirectory);
        for (var depth = 0; directory is not null && depth < 10; depth++, directory = directory.Parent)
        {
            if (HasMain(directory.FullName))
            {
                return directory.FullName;
            }
        }

        return null;
    }

    /// <summary>The configured Python, the repo's virtual environment, or plain "python" from PATH.</summary>
    public static string FindPython(string repoRoot, Func<string, string?>? env = null)
    {
        env ??= Environment.GetEnvironmentVariable;
        var configured = env(PythonVariable);
        if (!string.IsNullOrWhiteSpace(configured))
        {
            return configured;
        }

        var venv = Path.Combine(repoRoot, ".venv", "Scripts", "python.exe");
        return File.Exists(venv) ? venv : "python";
    }

    private static bool HasMain(string directory) => File.Exists(Path.Combine(directory, "desktop-client", "main.py"));
}
