namespace Irs990McpServer.Models;

/// <summary>
/// Loaded from dbo.mcp_clients after API key validation.
/// Stored in HttpContext.Items["McpClient"] for the lifetime of the request.
/// </summary>
public sealed record McpClient(
    int    Id,
    string ClientName,
    string ApiKeyPrefix,
    bool   IsActive
);

/// <summary>
/// Loaded from dbo.mcp_client_users when X-User-Id header is present.
/// Null if the user identifier isn't registered for this client.
/// </summary>
public sealed record McpClientUser(
    int    Id,
    int    ClientId,
    string UserIdentifier,
    string? DisplayName,
    bool   IsActive
);
