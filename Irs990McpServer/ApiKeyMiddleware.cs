using Irs990McpServer.Data;
using Irs990McpServer.Models;

namespace Irs990McpServer.Middleware;

/// <summary>
/// Validates the API key on every request to /mcp.
///
/// Clients send:
///   X-Api-Key:  {raw_api_key}        (required)
///   X-User-Id:  {email_or_username}  (optional — for per-user logging)
///
/// On success the following items are set in HttpContext.Items:
///   "McpClient"     → McpClient record
///   "McpClientUser" → McpClientUser record (null if X-User-Id absent or unregistered)
///   "McpUserId"     → raw string from X-User-Id header (null if absent)
///
/// /health is always allowed through (no key required).
/// </summary>
public sealed class ApiKeyMiddleware(
    RequestDelegate      next,
    McpClientRepository  clientRepo,
    ILogger<ApiKeyMiddleware> logger)
{
    private const string ApiKeyHeader  = "X-Api-Key";
    private const string UserIdHeader  = "X-User-Id";

    public async Task InvokeAsync(HttpContext context)
    {
        // Always let health probes through
        if (context.Request.Path.StartsWithSegments("/health"))
        {
            await next(context);
            return;
        }

        // ----------------------------------------------------------------
        // 1. Extract the API key
        // ----------------------------------------------------------------
        if (!context.Request.Headers.TryGetValue(ApiKeyHeader, out var keyValues)
            || string.IsNullOrWhiteSpace(keyValues))
        {
            logger.LogWarning("Request to {Path} rejected — missing {Header}. IP={IP}",
                context.Request.Path, ApiKeyHeader,
                context.Connection.RemoteIpAddress);

            context.Response.StatusCode = StatusCodes.Status401Unauthorized;
            await context.Response.WriteAsync("Unauthorized: X-Api-Key header is required.");
            return;
        }

        var rawKey = keyValues.ToString().Trim();
        var ct     = context.RequestAborted;

        // ----------------------------------------------------------------
        // 2. Validate against the database
        // ----------------------------------------------------------------
        var client = await clientRepo.GetClientByKeyAsync(rawKey, ct);

        if (client is null)
        {
            logger.LogWarning(
                "Request to {Path} rejected — unknown or inactive key prefix={Prefix}. IP={IP}",
                context.Request.Path,
                rawKey.Length >= 8 ? rawKey[..8] : "(short)",
                context.Connection.RemoteIpAddress);

            context.Response.StatusCode = StatusCodes.Status401Unauthorized;
            await context.Response.WriteAsync("Unauthorized: invalid or revoked API key.");
            return;
        }

        // ----------------------------------------------------------------
        // 3. Resolve the optional user identifier
        // ----------------------------------------------------------------
        string? userIdentifier = null;
        McpClientUser? clientUser = null;

        if (context.Request.Headers.TryGetValue(UserIdHeader, out var userIdValues)
            && !string.IsNullOrWhiteSpace(userIdValues))
        {
            userIdentifier = userIdValues.ToString().Trim();
            clientUser     = await clientRepo.FindClientUserAsync(client.Id, userIdentifier, ct);
        }

        // ----------------------------------------------------------------
        // 4. Store in HttpContext.Items for downstream middleware / logging
        // ----------------------------------------------------------------
        context.Items["McpClient"]     = client;
        context.Items["McpClientUser"] = clientUser;    // may be null
        context.Items["McpUserId"]     = userIdentifier; // raw string, may be null

        logger.LogDebug(
            "Authenticated client={ClientName} user={User}",
            client.ClientName, userIdentifier ?? "(none)");

        await next(context);
    }
}
