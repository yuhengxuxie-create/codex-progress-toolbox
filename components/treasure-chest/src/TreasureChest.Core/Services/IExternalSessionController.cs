using TreasureChest.Core.Models;

namespace TreasureChest.Core.Services;

/// <summary>A service whose persistent lifecycle is owned outside the desktop.</summary>
public interface IExternalSessionController
{
    bool Handles(SessionDefinition session);
    bool ShouldStop(SessionDefinition session);
    Task<SessionSnapshot> ProbeAsync(SessionDefinition session, CancellationToken cancellationToken);
    Task StartAsync(SessionDefinition session, CancellationToken cancellationToken);
    Task StopAsync(SessionDefinition session, CancellationToken cancellationToken);
    Task ExitAsync(CancellationToken cancellationToken);
}
