using System.IO;

/// <summary>
/// Resolves presentation-stage environment assets that are owned outside the Godot project.
///
/// Use when:
/// - The Godot stage is running from a workspace checkout and should reuse existing stage assets.
/// - A fixed visual preset needs a local filesystem path instead of a Godot imported resource.
///
/// Expects:
/// - Runtime and workspace directories are absolute paths supplied by the engine, independent of cwd.
///
/// Returns:
/// - Absolute filesystem paths for found assets, or an empty string when unavailable.
/// </summary>
public static class StageEnvironmentAssetResolver
{
    private static readonly string[] ThreeStageSkyHdriPathSegments = new[]
    {
        "packages",
        "stage-ui-three",
        "src",
        "components",
        "Environment",
        "assets",
        "sky_linekotsi_23_HDRI.hdr",
    };

    /// <summary>
    /// Resolves exported assets beside the executable or inside a macOS bundle.
    /// Editor runs can also supply an absolute workspace directory to reuse the source HDRI.
    /// Returns an empty string when the file is unavailable; relative inputs never use cwd.
    /// </summary>
    public static string ResolveThreeStageSkyHdriPath(string executableDirectory, string workspaceDirectory = null)
    {
        if (!string.IsNullOrWhiteSpace(executableDirectory) && Path.IsPathFullyQualified(executableDirectory))
        {
            var skyFileName = ThreeStageSkyHdriPathSegments[^1];
            var candidates = new[]
            {
                Path.Combine(executableDirectory, "assets", skyFileName),
                Path.Combine(executableDirectory, "..", "Resources", "assets", skyFileName),
            };
            foreach (var candidate in candidates)
            {
                if (File.Exists(candidate)) return Path.GetFullPath(candidate);
            }
        }

        // Only editor runs supply this anchor; an exported package cannot depend on a source checkout.
        if (string.IsNullOrWhiteSpace(workspaceDirectory) || !Path.IsPathFullyQualified(workspaceDirectory))
        {
            return string.Empty;
        }
        var currentDirectory = Path.GetFullPath(workspaceDirectory);
        while (!string.IsNullOrWhiteSpace(currentDirectory))
        {
            var candidatePath = Path.GetFullPath(Path.Combine(
                currentDirectory,
                Path.Combine(ThreeStageSkyHdriPathSegments)
            ));

            if (File.Exists(candidatePath))
            {
                return candidatePath;
            }

            // Walk upward from the Godot project directory until a repository root candidate matches.
            currentDirectory = Directory.GetParent(currentDirectory)?.FullName;
        }

        return string.Empty;
    }
}
