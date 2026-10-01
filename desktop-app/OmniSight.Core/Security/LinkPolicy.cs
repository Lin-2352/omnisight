using System.Net;
using System.Net.Sockets;

namespace OmniSight.Core.Security;

/// <summary>
/// Which links from model text or search results may be opened. Everything on screen can steer the model, so a link is only
/// followed when it is a plain http(s) address to a public host: never <c>file:</c>, a network share, <c>ms-settings:</c>,
/// <c>javascript:</c>, a name with credentials in it, or an address on this machine or its local network.
/// </summary>
public static class LinkPolicy
{
    public const int MaxLength = 2000;

    public static bool IsSafe(string? url) => TryNormalize(url, out _);

    /// <summary>The address to open, or false. The result is what <see cref="Uri.AbsoluteUri"/> gives, never the raw text.</summary>
    public static bool TryNormalize(string? url, out string normalized)
    {
        normalized = "";
        if (string.IsNullOrWhiteSpace(url) || url.Length > MaxLength || !url.Trim().Equals(url, StringComparison.Ordinal))
        {
            return false;
        }

        foreach (var ch in url)
        {
            if (char.IsControl(ch) || char.IsWhiteSpace(ch) || ch == '\\')
            {
                return false;
            }
        }

        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps)
            || uri.UserInfo.Length > 0
            || !url.StartsWith(uri.Scheme + "://", StringComparison.OrdinalIgnoreCase)
            || !IsPublicHost(uri))
        {
            return false;
        }

        normalized = uri.AbsoluteUri;
        return true;
    }

    private static bool IsPublicHost(Uri uri)
    {
        var host = uri.IdnHost.TrimEnd('.').ToLowerInvariant();
        if (host.Length == 0)
        {
            return false;
        }

        if (uri.HostNameType is UriHostNameType.IPv4 or UriHostNameType.IPv6)
        {
            return IPAddress.TryParse(uri.Host.Trim('[', ']'), out var ip) && IsPublicAddress(ip);
        }

        if (host == "localhost" || host.EndsWith(".localhost", StringComparison.Ordinal)
            || host.EndsWith(".local", StringComparison.Ordinal) || host.EndsWith(".internal", StringComparison.Ordinal)
            || host.EndsWith(".lan", StringComparison.Ordinal) || host.EndsWith(".home.arpa", StringComparison.Ordinal))
        {
            return false;
        }

        // A name without a dot is an intranet or shortcut name, never a public site.
        return host.Contains('.');
    }

    private static bool IsPublicAddress(IPAddress ip)
    {
        if (ip.IsIPv4MappedToIPv6)
        {
            ip = ip.MapToIPv4();
        }

        if (IPAddress.IsLoopback(ip) || ip.Equals(IPAddress.Any) || ip.Equals(IPAddress.IPv6Any) || ip.IsIPv6LinkLocal
            || ip.IsIPv6SiteLocal || ip.IsIPv6Multicast)
        {
            return false;
        }

        if (ip.AddressFamily == AddressFamily.InterNetworkV6)
        {
            var bytes = ip.GetAddressBytes();
            return (bytes[0] & 0xFE) != 0xFC;  // fc00::/7 unique local
        }

        var b = ip.GetAddressBytes();
        return !(b[0] == 10
                 || b[0] == 127
                 || b[0] == 0
                 || b[0] >= 224
                 || (b[0] == 169 && b[1] == 254)
                 || (b[0] == 172 && b[1] is >= 16 and <= 31)
                 || (b[0] == 192 && b[1] == 168)
                 || (b[0] == 100 && b[1] is >= 64 and <= 127));
    }
}
