from math import sqrt

import matplotlib.pyplot as plt
import torch
from matplotlib.collections import PolyCollection

torch.set_default_dtype(torch.double)


class Tria:
    def __init__(self):
        self.nodes = 3

    def N(self, xi):
        N_1 = 1.0 - xi[..., 0] - xi[..., 1]
        N_2 = xi[..., 0]
        N_3 = xi[..., 1]
        return torch.stack([N_1, N_2, N_3], dim=2)

    def B(self, _):
        return torch.tensor([[-1.0, 1.0, 0.0], [-1.0, 0.0, 1.0]])

    def ipoints(self):
        return [[1.0 / 3.0, 1.0 / 3.0]]

    def iweights(self):
        return [0.5]


class Quad:
    def __init__(self):
        self.nodes = 4

    def N(self, xi):
        N_1 = (1.0 - xi[..., 0]) * (1.0 - xi[..., 1])
        N_2 = (1.0 + xi[..., 0]) * (1.0 - xi[..., 1])
        N_3 = (1.0 + xi[..., 0]) * (1.0 + xi[..., 1])
        N_4 = (1.0 - xi[..., 0]) * (1.0 + xi[..., 1])
        return 0.25 * torch.stack([N_1, N_2, N_3, N_4], dim=2)

    def B(self, xi):
        return 0.25 * torch.tensor(
            [
                [-(1.0 - xi[1]), (1.0 - xi[1]), (1.0 + xi[1]), -(1.0 + xi[1])],
                [-(1.0 - xi[0]), -(1.0 + xi[0]), (1.0 + xi[0]), (1.0 - xi[0])],
            ]
        )

    def ipoints(self):
        return [
            [xi_1 / sqrt(3.0), xi_2 / sqrt(3.0)]
            for xi_2 in [-1.0, 1.0]
            for xi_1 in [-1.0, 1.0]
        ]

    def iweights(self):
        return [1.0, 1.0, 1.0, 1.0]


class FEM:
    def __init__(self, nodes, elements, forces, constraints, thickness, E, nu):
        # Nodes
        self.nodes = nodes
        self.n_dofs = torch.numel(self.nodes)
        # Elements
        self.elements = elements
        self.n_elem = len(self.elements)
        # Forces and constraints
        self.forces = forces
        self.constraints = constraints
        # Element thicknesses
        self.thickness = thickness
        # Element type
        if len(elements[0]) == 4:
            self.etype = Quad()
        else:
            self.etype = Tria()

        # Plain stress state
        self.C = (E / ((1.0 + nu) * (1.0 - 2.0 * nu))) * torch.tensor(
            [[1.0 - nu, nu, 0.0], [nu, 1.0 - nu, 0.0], [0.0, 0.0, 0.5 - nu]]
        )

        # Precompute distance between element centers (for filtering)
        ecenters = torch.stack([torch.mean(nodes[e], dim=0) for e in elements])
        self.dist = torch.cdist(ecenters, ecenters)
        # Precompute global indices and element stiffness matrices
        gidx_1 = []
        gidx_2 = []
        for j, element in enumerate(self.elements):
            # Compute efficient mapping from local to global indices
            indices = torch.tensor([2 * n + i for n in element for i in range(2)])
            idx_1, idx_2 = torch.meshgrid(indices, indices, indexing="xy")
            gidx_1.append(idx_1)
            gidx_2.append(idx_2)
        self.gidx_1 = torch.stack(gidx_1)
        self.gidx_2 = torch.stack(gidx_2)

        self.k0 = torch.einsum("i,ijk->ijk", 1.0 / self.thickness, self.k())

    def k(self):
        # Perform integrations
        nodes = self.nodes[self.elements, :]
        k = torch.zeros((self.n_elem, 2 * self.etype.nodes, 2 * self.etype.nodes))
        for w, q in zip(self.etype.iweights(), self.etype.ipoints()):
            # Jacobian
            J = self.etype.B(q) @ nodes
            detJ = torch.linalg.det(J)
            if torch.any(detJ <= 0.0):
                raise Exception("Negative Jacobian. Check element numbering.")
            # Element stiffness
            B = torch.linalg.inv(J) @ self.etype.B(q)
            zeros = torch.zeros(self.n_elem, self.etype.nodes)
            D0 = torch.stack([B[:, 0, :], zeros], dim=-1).reshape(self.n_elem, -1)
            D1 = torch.stack([zeros, B[:, 1, :]], dim=-1).reshape(self.n_elem, -1)
            D2 = torch.stack([B[:, 1, :], B[:, 0, :]], dim=-1).reshape(self.n_elem, -1)
            D = torch.stack([D0, D1, D2], dim=1)
            DCD = torch.einsum("...ji,...jk,...kl->...il", D, self.C, D)
            k[:, :, :] += torch.einsum("i,ijk->ijk", w * self.thickness * detJ, DCD)
        return k

    def areas(self):
        areas = torch.zeros((self.n_elem))
        # Perform integrations
        nodes = self.nodes[self.elements, :]
        for w, q in zip(self.etype.iweights(), self.etype.ipoints()):
            # Jacobian
            J = self.etype.B(q) @ nodes
            detJ = torch.linalg.det(J)
            # Area integration
            areas[:] += w * detJ
        return areas

    def element_strain_energies(self, u):
        # Compute strain energies of all elements
        w = torch.zeros((self.n_elem))
        for j, element in enumerate(self.elements):
            u_j = torch.tensor([u[int(n), i] for n in element for i in [0, 1]])
            w[j] = 0.5 * u_j @ self.k0[j] @ u_j
        return w

    def stiffness(self):
        # Assemble global stiffness matrix
        K = torch.zeros((self.n_dofs, self.n_dofs))
        K.index_put_((self.gidx_1, self.gidx_2), self.k(), accumulate=True)
        return K

    def solve(self):
        # Compute global stiffness matrix
        K = self.stiffness()

        # Get reduced stiffness matrix
        uncon = torch.nonzero(~self.constraints.ravel(), as_tuple=False).ravel()
        K_red = torch.index_select(K, 0, uncon)
        K_red = torch.index_select(K_red, 1, uncon)
        f_red = self.forces.ravel()[uncon]

        # Solve for displacement
        u_red = torch.linalg.solve(K_red, f_red)
        u = torch.zeros_like(self.nodes).ravel()
        u[uncon] = u_red

        # Evaluate force
        f = K @ u

        u = u.reshape((-1, 2))
        f = f.reshape((-1, 2))
        return u, f

    @torch.no_grad()
    def plot(
        self,
        u=0.0,
        node_property=None,
        element_property=None,
        node_labels=False,
        cmap="gray_r",
    ):
        # Compute deformed positions
        pos = self.nodes + u

        # Bounding box
        size = torch.linalg.norm(pos.max() - pos.min())

        # Color surface with interpolated nodal properties (if provided)
        if node_property is not None:
            if type(self.etype) == Quad:
                triangles = []
                for e in self.elements:
                    triangles.append([e[0], e[1], e[2]])
                    triangles.append([e[2], e[3], e[0]])
            else:
                triangles = self.elements
            plt.tricontourf(pos[:, 0], pos[:, 1], triangles, node_property)

        # Color surface with element properties (if provided)
        if element_property is not None:
            ax = plt.gca()
            verts = pos[self.elements]
            pc = PolyCollection(verts, cmap=cmap)
            pc.set_array(element_property)
            ax.add_collection(pc)



        # Nodes
        if len(pos) < 20000:
            plt.scatter(pos[:, 0], pos[:, 1], color="black", marker="o", s=1)
            if node_labels:
                for i, node in enumerate(pos):
                    plt.annotate(
                        i, (node[0] + 0.001 * size, node[1] + 0.001 * size), color="black"
                    )
                plt.savefig("testfile.jpg", format="jpg", dpi="1500")

        # Elements
        for element in self.elements:
            x1 = [pos[node, 0] for node in element] + [pos[element[0], 0]]
            x2 = [pos[node, 1] for node in element] + [pos[element[0], 1]]
            plt.plot(x1, x2, color="black")


        # Forces
        for i, force in enumerate(self.forces):
            if torch.norm(force) > 0.0:
                x = pos[i][0]
                y = pos[i][1]
                plt.arrow(
                    x,
                    y,
                    size * 0.05 * force[0] / torch.norm(force),
                    size * 0.05 * force[1] / torch.norm(force),
                    width=0.01 * size,
                    facecolor="gray",
                    zorder=10,
                )

        # Constraints
        for i, constraint in enumerate(self.constraints):
            x = pos[i][0]
            y = pos[i][1]
            if constraint[0]:
                plt.plot(x - 0.01 * size, y, ">", color="gray")
            if constraint[1]:
                plt.plot(x, y - 0.01 * size, "^", color="gray")

        plt.gca().set_aspect("equal", adjustable="box")
        plt.axis("off")


def get_cantilever(size, Lx, Ly, d=1.0, E=100, nu=0.3, etype=Quad()):
    # Dimensions
    Nx = int(Lx / size)
    Ny = int(Ly / size)

    # Create nodes
    n1 = torch.linspace(0.0, Lx, Nx + 1)
    n2 = torch.linspace(0.0, Ly, Ny + 1)
    n1, n2 = torch.stack(torch.meshgrid(n1, n2, indexing="xy"))
    nodes = torch.stack([n1.ravel(), n2.ravel()], dim=1)

    # Create elements connecting nodes
    elements = []
    for j in range(Ny):
        for i in range(Nx):
            if type(etype) == Quad:
                # Quad elements
                n0 = i + j * (Nx + 1)
                elements.append([n0, n0 + 1, n0 + Nx + 2, n0 + Nx + 1])
            else:
                # Tria elements
                n0 = i + j * (Nx + 1)
                elements.append([n0, n0 + 1, n0 + Nx + 2])
                elements.append([n0 + Nx + 2, n0 + Nx + 1, n0])

    # Load at tip
    forces = torch.zeros_like(nodes)
    # forces[-1, 1] = -1.0
    forces[(int((Ny + 1) / 2) + 1) * (Nx + 1) - 1, 1] = -1.0

    # Constrained displacement at left end
    constraints = torch.zeros_like(nodes, dtype=bool)
    for i in range(Ny + 1):
        constraints[i * (Nx + 1), :] = True

    # Default
    thickness = d * torch.ones(len(elements))

    return FEM(nodes, elements, forces, constraints, thickness, E, nu)


@torch.no_grad()
def export_mesh(fem, filename, nodal_data={}, elem_data={}):
    from meshio import Mesh

    if type(fem.etype) == Quad:
        etype = "quad"
    elif type(fem.etype) == Tria:
        etype = "triangle"

    mesh = Mesh(
        points=fem.nodes,
        cells={etype: fem.elements},
        point_data=nodal_data,
        cell_data=elem_data,
    )
    mesh.write(filename)


def import_mesh(filename, E, nu):
    import meshio
    import numpy as np

    mesh = meshio.read(filename)
    nodes = torch.from_numpy(mesh.points[:, 0:2].astype(np.float64))
    elements = []
    for cell_block in mesh.cells:
        if cell_block.type in ["triangle", "quad"]:
            elements += cell_block.data.tolist()
    forces = torch.zeros_like(nodes)
    constraints = torch.zeros_like(nodes, dtype=bool)
    thickness = torch.ones((len(elements)))

    return FEM(nodes, elements, forces, constraints, thickness, E, nu)