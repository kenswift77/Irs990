using System.Security.Cryptography;
using System.Text;
using Dapper;
using Irs990McpServer.Models;
using Microsoft.Data.SqlClient;

namespace Irs990McpServer.Data;

/// <summary>
/// DB access for MCP client authentication and request logging.
/// </summary>
public sealed class McpClientRepository(IConfiguration config, ILogger<McpClientRepository> logger)
{
    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    private SqlConnection OpenSync()
    {
        var connStr = config.GetConnectionString("Irs990Db")
                      ?? throw new InvalidOperationException("ConnectionStrings:Irs990Db is required.");
        var conn = new SqlConnection(connStr);
        conn.Open();
        return conn;
    }

    /// <summary>
    /// Computes the SHA-256 hex string of the raw API key — matches the
    /// value stored by irs990_mcp_generate_key.sql (HASHBYTES / hex style 2).
    /// </summary>
    public static string HashKey(string rawKey)
    {
        var bytes = SHA256.HashData(Encoding.UTF8.GetBytes(rawKey));
        return Convert.ToHexString(bytes); // upper-case hex, matches SQL style 2
    }

    // -----------------------------------------------------------------------
    // Authentication
    // -----------------------------------------------------------------------

    /// <summary>
    /// Looks up the client whose api_key_hash matches the given raw key.
    /// Returns null if not found or inactive.
    /// </summary>
    public async Task<McpClient?> GetClientByKeyAsync(string rawKey, CancellationToken ct)
    {
        var hash = HashKey(rawKey);

        const string sql = """
            SELECT id AS Id, client_name AS ClientName,
                   api_key_prefix AS ApiKeyPrefix, is_active AS IsActive
            FROM dbo.mcp_clients
            WHERE api_key_hash = @hash AND is_active = 1
            """;

        try
        {
            await using var conn = OpenSync();
            return await conn.QueryFirstOrDefaultAsync<McpClient>(
                new CommandDefinition(sql, new { hash }, cancellationToken: ct));
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Error looking up MCP client by key hash");
            return null;
        }
    }

    // -----------------------------------------------------------------------
    // User resolution
    // -----------------------------------------------------------------------

    /// <summary>
    /// Finds a registered user within a client by their identifier string
    /// (e.g. email address sent in X-User-Id header).
    /// Returns null if not found — the raw identifier is still logged.
    /// </summary>
    public async Task<McpClientUser?> FindClientUserAsync(
        int clientId, string userIdentifier, CancellationToken ct)
    {
        const string sql = """
            SELECT id AS Id, client_id AS ClientId,
                   user_identifier AS UserIdentifier,
                   display_name AS DisplayName, is_active AS IsActive
            FROM dbo.mcp_client_users
            WHERE client_id = @clientId
              AND user_identifier = @userIdentifier
              AND is_active = 1
            """;

        try
        {
            await using var conn = OpenSync();
            return await conn.QueryFirstOrDefaultAsync<McpClientUser>(
                new CommandDefinition(sql, new { clientId, userIdentifier }, cancellationToken: ct));
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Error looking up MCP client user {User}", userIdentifier);
            return null;
        }
    }

    // -----------------------------------------------------------------------
    // Request logging
    // -----------------------------------------------------------------------

    /// <summary>
    /// Inserts one row into dbo.mcp_request_log.
    /// Fire-and-forget safe — errors are swallowed so a logging failure
    /// never breaks the MCP response.
    /// </summary>
    public async Task LogRequestAsync(
        int    clientId,
        int?   clientUserId,
        string? userIdentifier,
        string? toolName,
        int    durationMs,
        string status,          // "success" | "error"
        string? ipAddress,
        string? errorMessage,
        CancellationToken ct)
    {
        const string sql = """
            INSERT INTO dbo.mcp_request_log
                (client_id, client_user_id, user_identifier,
                 tool_name, duration_ms, status, ip_address, error_message)
            VALUES
                (@clientId, @clientUserId, @userIdentifier,
                 @toolName, @durationMs, @status, @ipAddress, @errorMessage)
            """;

        try
        {
            await using var conn = OpenSync();
            await conn.ExecuteAsync(new CommandDefinition(sql, new
            {
                clientId,
                clientUserId,
                userIdentifier,
                toolName     = toolName?[..Math.Min(toolName.Length, 100)],
                durationMs,
                status,
                ipAddress    = ipAddress?[..Math.Min(ipAddress?.Length ?? 0, 45)],
                errorMessage = errorMessage?[..Math.Min(errorMessage?.Length ?? 0, 1000)]
            }, cancellationToken: ct));
        }
        catch (Exception ex)
        {
            // Logging must never break the actual MCP response
            logger.LogWarning(ex, "Failed to write MCP request log row");
        }
    }
}
