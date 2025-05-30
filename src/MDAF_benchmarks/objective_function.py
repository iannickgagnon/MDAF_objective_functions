import inspect
import logging
import os
import pickle
import re
import timeit
from abc import ABC, abstractmethod
from concurrent.futures import ProcessPoolExecutor, as_completed
from numbers import Number
from typing import Callable, Iterable, Optional

import matplotlib.pyplot as plt
import numpy as np
from autograd import grad, hessian
from line_profiler import LineProfiler
from src.MDAF_benchmarks.default_settings import DefaultSettings

# Internal constants
LEFT_CLICK = 1
RIGHT_CLICK = 3

# Internal paths
DECOUPLED_FUNCTION_PATH = "./src/objective_functions/tmp/decoupled_evaluate.py"

# Create module-specific logger with default warning level set to WARNING
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


class ColorFormatter(logging.Formatter):
    """
    A custom logging formatter that adds ANSI color codes to log messages based on their severity level.

    Attributes:
        COLORS (dict): A mapping from log level names to their corresponding ANSI color codes.

    Methods:
        format(record):
            Formats the specified log record with the appropriate color based on its level.

    Args:
        record (logging.LogRecord): The log record to be formatted.

    Returns:
        str: The formatted log message string with ANSI color codes applied.
    """

    COLORS = {
        "WARNING": "\033[93m",
        "INFO": "\033[92m",
        "ERROR": "\033[91m",
        "DEBUG": "\033[94m",
    }

    def format(self, record: logging.LogRecord) -> str:
        """
        Formats the log record with an appropriate color based on its level.

        Args:
            record (logging.LogRecord): The log record to be formatted.

        Returns:
            str: The formatted log message string with color codes applied.
        """

        # Get the ASCII color code for the log level
        color = self.COLORS.get(record.levelname, "")

        # Format the message with the color code by sending it to the formatter
        message = super().format(record)

        return f"{color}{message}'\033[0m"


# Create a console handler for color formatting
console_handler = logging.StreamHandler()
console_handler.setFormatter(ColorFormatter("%(levelname)s: %(message)s"))
logger.addHandler(console_handler)


def set_verbose_warnings(enabled: bool = True) -> None:
    """
    Enable or disable warning messages.

    Args:
        enabled (bool, optional): If True, enable warning messages. If False, disable them. Defaults to True.

    Returns:
        None
    """
    logger.setLevel(logging.WARNING if enabled else logging.ERROR)


def count_calls(foo: Callable) -> Callable:
    """
    Decorator that increments and stores the number of calls to a method.

    Args:
        foo (Callable): The function to count the number of calls to.

    Returns:
        Callable: The decorated function.
    """

    def wrapper(self, *args, **kwargs):

        # Increment the number of calls
        self.nb_calls += 1

        # Call the original function
        return foo(self, *args, **kwargs)

    return wrapper


def constructor(foo: Callable):
    """
    Calls the super constructor after executing the subclass constructor.

    Args:
        foo (Callable): The subclass constructor to be executed before calling the super constructor.

    Returns:
        Callable: The wrapper function that executes the given function and calls the super constructor.

    """

    def wrapper(self, **kwargs):

        # Initialize parameters
        self.parameters = {}

        # Call the subclass constructor
        foo(self, **kwargs)

        # Call the super constructor
        super(self.__class__, self).__init__(**kwargs)

    return wrapper


class ObjectiveFunction(ABC):

    def __init__(self):

        # Measure the dimensionality of the provided optimal solution position
        if np.any(self.optimal_solution_position):

            if isinstance(self.optimal_solution_position[0], Iterable):
                optimal_solution_dim = len(self.optimal_solution_position[0])
            else:
                optimal_solution_dim = len(self.optimal_solution_position)

            # Validate the size of the optimal solution position
            if (
                self.optimal_solution_position is not None
                and optimal_solution_dim != self.ndim
            ):
                raise ValueError(
                    "The size of the optimal position must match the dimensionality."
                )

            # Validate the size of the search space bounds
            if len(self.search_space_bounds) != self.ndim:
                raise ValueError(
                    "The size of the search space bounds must match the dimensionality of the objective function."
                )

        # Initialize shift
        self.shift: np.ndarray = np.zeros(self.ndim)
        self.noise_mean: float = 0.0
        self.noise_variance: float = 0.0

        # Initialize the number of objective function evaluations
        self.nb_calls: int = 0

        # Compute the first and second derivatives of the objective function's evaluation method
        self.first_derivative = grad(self.__evaluate)
        self.second_derivative = hessian(self.__evaluate)

    def validate_parameters(self, parameters: dict, default_params: dict):
        """
        Validates the parameters of the objective function.

        Args:
            parameters (dict): The parameters to validate.
            default_params (dict): The default parameters of the objective function.

        Returns:
            dict: The validated parameters.
        """

        # Check if the number of parameters exceeds the number of default parameters
        if len(parameters) > len(default_params):
            raise ValueError(
                "The number of parameters exceeds the number of default parameters."
            )

        # Make sure that all the provided parameters are also in the default parameters
        for parameter_name in parameters:
            if parameter_name not in default_params:
                raise ValueError(
                    f"'{parameter_name}' is not a valid parameter. Valid parameters are {default_params.keys()}."
                )

        # Store the parameters and set default values as required
        for parameter_name in default_params:
            if parameter_name in parameters:
                self.parameters[parameter_name] = parameters[parameter_name]
            else:
                default_value = default_params[parameter_name]
                logger.warning(
                    f"The '{parameter_name}' parameter is not set. Default value of {default_value} is used instead."
                )
                self.parameters[parameter_name] = default_value

    def validate_settings(self, settings: dict, default_settings: DefaultSettings):
        """
        Validates the settings of the objective function.

        Args:
            settings (dict): The settings to validate.
            default_settings (dict): The default settings of the objective function.

        Returns:
            dict: The validated settings.
        """

        # Store the settings and set default values as required
        for setting_name in default_settings:
            if setting_name in settings:
                self.__dict__[setting_name] = settings[setting_name]
            else:
                default_value = default_settings[setting_name]
                logger.warning(
                    f"The '{setting_name}' setting is not set. The default value of {default_value} is used."
                )
                self.__dict__[setting_name] = default_value

    @abstractmethod
    def evaluate(self, position: np.ndarray) -> float:
        """
        Evaluates the objective function at the given position.

        Args:
            solution (np.ndarray): The solution to evaluate.

        Returns:
            float: The objective function value at the given solution.
        """
        pass

    @count_calls
    def __evaluate(self, position: np.ndarray) -> float:
        """
        Evaluates the objective function at the given position. Adds noise and shift if specified

        Args:
            solution (np.ndarray): The solution to evaluate.

        Returns:
            float: The objective function value at the given solution.
        """
        return (
            self.evaluate(position - self.shift)
            + self.noise_mean
            + np.random.randn(position.shape[0]) * self.noise_variance
        )

    def time(
        self, nb_runs: int = 10000, output: bool = False
    ) -> list[float, (float, float)]:
        """
        Measures the execution time of the evaluate method and calculates a 95% bootstrap confidence interval.

        Args:
            nb_runs (int, optional): The number of times to run the evaluate method. Defaults to 10000.
            output (bool, optional): Whether to return the execution time (True) and CI or print it (False). Defaults to False.

        Returns:
            list[float, (float, float)]: The average execution time of the evaluate method a 95% bootstrap confidence interval.
        """

        # Wrap the evaluation function
        def wrapper():
            position = np.array(
                [np.random.uniform(low, high) for low, high in self.search_space_bounds]
            )
            return self.evaluate(position)

        # Generate the bootstrap sample
        bootstrap_times = np.array(
            [timeit.timeit(wrapper, number=1) for _ in range(nb_runs)]
        )

        # Calculate the average execution time
        mean_time = np.mean(bootstrap_times)

        # Calculate the 95% confidence interval
        lower_bound = np.percentile(bootstrap_times, 2.5)
        upper_bound = np.percentile(bootstrap_times, 97.5)

        if output:
            return [mean_time, (lower_bound, upper_bound)]
        else:
            logger.info(
                f"Execution time (n={nb_runs}): {mean_time:.3e} 95% CI ({lower_bound:.3e}, {upper_bound:.3e})"
            )

    def parallel_evaluate(
        self, positions: np.ndarray, max_workers: int = None
    ) -> np.ndarray:
        """
        Evaluates multiple positions in parallel.

        Args:
            positions (np.ndarray): An array of positions to evaluate. Each row corresponds to a position.
            max_workers (int): The maximum number of processes that can be used to execute the given calls.

        Returns:
            np.ndarray: An array of objective function values corresponding to the input positions.
        """

        try:

            # Export the evaluate function as a top-level function in a separate file
            self.__decouple_evaluate()

            # Import the decoupled evaluate function
            if os.path.exists(DECOUPLED_FUNCTION_PATH):
                try:
                    from MDAF_benchmarks.tmp.decoupled_evaluate import (  # type: ignore
                        evaluate as decoupled_evaluate,
                    )
                except Exception as e:
                    raise ImportError(
                        f"Failed to import the decoupled evaluate method with traceback: {e}"
                    )

        except Exception as e:

            # Delete the decoupled evaluate function file
            self.__delete_decoupled_evaluate()

            raise Exception(
                f"Failed to decouple the evaluate method with traceback: {e}"
            )

        with ProcessPoolExecutor(max_workers=max_workers) as executor:

            # Submit all evaluations to the executor
            future_to_index = {
                executor.submit(decoupled_evaluate, pos): i
                for i, pos in enumerate(positions)
            }

            # Pre-fill with NaNs to mark failed positions clearly
            results = np.full(len(positions), np.nan)

            # Collect results as they are completed
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    result = future.result()
                except Exception as e:
                    logger.error(
                        f"Position at index {index} generated an exception: {e}"
                    )
                else:
                    results[index] = result

        # Delete the decoupled evaluate function file
        self.__delete_decoupled_evaluate()

        return results

    def visualize(
        self,
        dimensions: Iterable[int] = (0, 1),
        plot_bounds: Iterable[Iterable[Number]] = None,
        resolution: int = 100,
        plot_2d_kwargs: Optional[dict] = None,
        plot_3d_kwargs: Optional[dict] = None,
    ) -> None:
        """
        Visualizes the objective function in 2D or 3D.

        Args:
            dimensions (Iterable[int]): The dimensions to visualize. Must be an iterable of length 2 or 3.
            bounds (Iterable[Iterable[Number]]): The bounds of the visualization. Must be an iterable of length 2 or 3.
            resolution (int): The resolution of the visualization grid.
            plot_2d_kwargs (dict, optional): Additional keyword arguments for the 2D plot. Default is None.
            plot_3d_kwargs (dict, optional): Additional keyword arguments for the 3D plot. Default is None.

        Raises:
            AssertionError: If the number of dimensions is not 2 or 3.
            AssertionError: If the number of bounds does not match the number of dimensions.
        """

        # Initialize defaults
        if plot_2d_kwargs is None:
            plot_2d_kwargs = {}

        if plot_3d_kwargs is None:
            plot_3d_kwargs = {}

        # Validate dimensions
        if len(dimensions) not in (1, 2):
            raise ValueError("The number of dimensions to visualize must be 1 or 2.")

        # Adjust the plot bounds
        if plot_bounds and len(plot_bounds) != len(dimensions):
            raise ValueError(
                "The number of bounds must match the number of dimensions."
            )
        elif not plot_bounds:
            plot_bounds = self.search_space_bounds

        if self.ndim == 1 or len(dimensions) == 1:

            # Create the figure and axis
            fig, ax = plt.subplots(figsize=(8, 6))

            # Define the grid for the single dimension
            x = np.linspace(plot_bounds[0][0], plot_bounds[0][1], resolution)

            # Vectorized evaluation of the objective function
            y = self.__evaluate(x)

            # Draw the line plot
            ax.plot(x, y)
            ax.set_xlabel(f"X{dimensions[0]}")
            ax.set_ylabel("Fitness")
            ax.grid(True, linestyle="--", alpha=0.5)

            plt.show()

        elif len(dimensions) == 2:

            # Default colormap
            if "cmap" not in plot_2d_kwargs:
                plot_2d_kwargs["cmap"] = "jet"

            if "cmap" not in plot_3d_kwargs:
                plot_3d_kwargs["cmap"] = "jet"

            # Create the figure and axes
            fig, axs = plt.subplots(1, 2, figsize=(13, 6))

            # Define the grid
            x_min, x_max = plot_bounds[0]
            y_min, y_max = plot_bounds[1]
            x = np.linspace(x_min, x_max, resolution)
            y = np.linspace(y_min, y_max, resolution)
            X, Y = np.meshgrid(x, y)

            # Vectorized evaluation of the objective function
            positions = np.vstack([X.ravel(), Y.ravel()]).T

            Z = np.array(
                [self.__evaluate(position.reshape(1, -1)) for position in positions]
            ).reshape(X.shape)

            # Draw the contour plot with level curves
            levels = np.linspace(np.min(Z), np.max(Z), num=min(resolution // 10, 10))
            cs = axs[0].contourf(X, Y, Z, levels=levels, **plot_2d_kwargs)
            axs[0].contour(cs, colors="k", linewidths=1.0)
            axs[0].set_xlabel(f"X{dimensions[0]}")
            axs[0].set_ylabel(f"X{dimensions[1]}")

            # Show the optimal solution on the contour plot
            if self.optimal_solution_position is not None:
                if isinstance(self.optimal_solution_position[0], Iterable):
                    for position in self.optimal_solution_position:
                        axs[0].scatter(
                            position[dimensions[0]],
                            position[dimensions[1]],
                            color="yellow",
                            marker="*",
                            edgecolor="black",
                            s=200,
                        )
                else:
                    axs[0].scatter(
                        self.optimal_solution_position[dimensions[0]],
                        self.optimal_solution_position[dimensions[1]],
                        color="yellow",
                        marker="*",
                        edgecolor="black",
                        s=200,
                    )

            plt.tight_layout()

            def on_contour_click(event) -> None:
                """
                Event handler for mouse clicks on the contour plot. Adds a red sphere marker on
                the 3D plot for a left-click and removes the last marker for a right-click.

                Args:
                    event (MouseEvent): The mouse click event.

                Returns:
                    None
                """

                if event.button == LEFT_CLICK:

                    # Get the clicked coordinates
                    x, y = event.xdata, event.ydata

                    # This is a workaround to avoid a 'bug' in matplotlib where a left-click event is always triggered around (0, 0)
                    if x is None or x is None or (np.abs(x) < 0.1 and np.abs(y) < 0.1):
                        return

                    # Create a sphere marker on the 3D plot for a left-click
                    axs[1].scatter(
                        x,
                        y,
                        self.__evaluate(np.array([[x, y]])),
                        color="red",
                        marker="o",
                        s=100,
                    )

                if event.button == RIGHT_CLICK:

                    # Remove the previous sphere marker on the 3D plot for a right-click
                    if len(axs[1].collections) > 2:
                        axs[1].collections[-1].remove()

                plt.draw()

            # Register the on_contour_click function as the callback for the contour plot
            cs.figure.canvas.mpl_connect("button_press_event", on_contour_click)

            # 3D surface visualization
            axs[1].axis("off")
            axs[1] = fig.add_subplot(122, projection="3d")
            axs[1].plot_surface(X, Y, Z, **plot_3d_kwargs)
            axs[1].set_xlabel(f"X{dimensions[0]}")
            axs[1].set_ylabel(f"X{dimensions[1]}")
            axs[1].set_zlabel(f"f(X{dimensions[0]}, X{dimensions[1]})")

            # Show the optimal solution on the surface plot
            if self.optimal_solution_position is not None:
                if isinstance(self.optimal_solution_position[0], Iterable):
                    for position in self.optimal_solution_position:
                        axs[1].scatter(
                            position[dimensions[0]],
                            position[dimensions[1]],
                            self.optimal_solution,
                            color="yellow",
                            marker="*",
                            edgecolor="black",
                            s=200,
                        )
                else:
                    axs[1].scatter(
                        self.optimal_solution_position[dimensions[0]],
                        self.optimal_solution_position[dimensions[1]],
                        self.optimal_solution,
                        color="yellow",
                        marker="*",
                        edgecolor="black",
                        s=200,
                    )

            # Adjust the layout
            plt.tight_layout()

            plt.show()

    def compute_first_derivative(self, position: np.ndarray) -> np.ndarray:
        """
        Computes the first derivative of the objective function at the given position.

        Args:
            position (np.ndarray): The position at which to compute the first derivative.

        Returns:
            np.ndarray: The first derivative of the objective function at the given position.
        """
        return self.first_derivative(position)

    def compute_second_derivative(self, position: np.ndarray) -> np.ndarray:
        """
        Computes the second derivative of the objective function at the given position.

        Args:
            position (np.ndarray): The position at which to compute the second derivative.

        Returns:
            np.ndarray: The second derivative of the objective function at the given position.
        """
        return self.second_derivative(position)

    def check_constraints(self, position: np.ndarray) -> bool:
        """
        Checks if the given solution satisfies the constraints defined by the search space bounds.

        Args:
            solution (np.ndarray): The solution to check.

        Returns:
            bool: True if the solution satisfies the constraints, False otherwise.

        Raises:
            ValueError: If no constraints on the search space have been defined for this objective function.
        """

        # Check if the search space bounds have been defined
        if not self.search_space_bounds:
            raise ValueError(
                "No constraints on the search space have been defined for this objective function."
            )

        # Check if the solution satisfies the constraints for each dimension
        return np.any(
            (position < self.search_space_bounds[:, 0])
            | (position > self.search_space_bounds[:, 1])
        )

    def apply_shift(self, shift: np.ndarray) -> None:
        """
        Shifts the given position by the given shift vector.

        Args:
            shift (np.ndarray): The shift vector.

        Returns:
            np.ndarray: The shifted position.
        """

        # Shift the optimal solution position
        if self.optimal_solution_position is not None:
            self.optimal_solution_position += shift

        # Store the shift vector
        self.shift = shift

    def apply_noise(self, mean: float = 0.0, variance: float = 0.1) -> None:
        """
        Applies Gaussian noise to the objective function.

        Args:
            mean (float, optional): The mean of the Gaussian noise. Defaults to 0.0.
            variance (float, optional): The variance of the Gaussian noise. Defaults to 0.1.

        Returns:
            Nothing
        """

        # Store the noise parameters
        self.noise_mean = mean
        self.noise_variance = variance

    def save(self, path: str) -> None:
        """
        Saves the objective function to a file.

        Args:
            path (str): The path to save the objective function to.
        """
        with open(path, "wb") as file:
            pickle.dump(self, file)
            logger.info("ObjectiveFunction state saved in {path}")

    @staticmethod
    def load(path: str):
        """
        Deserialize the state of the objective function from a file.

        Args:
            filename (str): The path to the file from which the object state will be loaded.

        Returns:
            ObjectiveFunction: The deserialized objective function object.
        """
        with open(path, "rb") as f:
            obj = pickle.load(f)
            logger.info("ObjectiveFunction state loaded from {path}")
            return obj

    def __remove_self_references(self, code: str) -> str:
        """
        Removes self-references from the given code by replacing 'self.parameters['...']' with corresponding literal values.

        Args:
            code (str): The code string to remove self-references from.

        Returns:
            str: The modified code string with self-references removed.
        """

        # Find all the self-references in the code
        match_expr = re.findall("self.parameters\[\s*'\s*.\s*'\s*\]", code)
        match_para = re.findall("self.parameters\[\s*'\s*(.)\s*'\s*\]", code)

        # Replace the self-references with the corresponding literal values
        for para, expr in zip(match_para, match_expr):
            code = code.replace(expr, str(self.parameters[para]))

        return code

    def __remove_docstrings(self, code: str) -> str:
        """
        Removes docstrings from the given code.

        Args:
            code (str): The code string to remove docstrings from.

        Returns:
            str: The modified code string with docstrings removed.
        """
        code = re.sub(r"\"\"\".*?\"\"\"", "", code, flags=re.DOTALL)
        code = re.sub(r"'''.*?'''", "", code, flags=re.DOTALL)
        return code

    def __decouple_shift_noise(self, code: str) -> str:
        """
        Adds shift and noise to the decoupled evaluate method.

        Args:
            code (str): The decoupled evaluate method code.

        Returns:
            str: The modified code with shift and noise added.
        """

        if any(self.shift):

            # Find the def statement
            def_line = re.search("def.*\n", code)[0]

            # Build shift expression
            shift_str = f"np.array({self.shift})".replace(" ", ",")

            # Add the shift right after the original def statement
            code = code.replace(def_line, def_line + f"    position += {shift_str}\n")

        if self.noise_mean or self.noise_variance:

            # Find the return statement
            return_line = re.search("return.*", code)

            # Make sure the return statement exists
            if not return_line:
                raise ValueError(
                    "Could not find the return statement in the evaluate method."
                )

            # Make a copy of the return line
            return_line_copy = return_line[0]

            # Remove the last line feed(s) and combine
            new_return_line = (
                return_line_copy.replace("\n", "")
                + f" + {self.noise_mean} + np.random.randn() * {self.noise_variance}"
            )

            # Replace the original return line with the modified one
            code = code.replace(return_line_copy, new_return_line)

        return code

    def __decouple_evaluate(self) -> None:
        """
        Decouples the evaluate method from the objective function instance.

        Args:
            None

        Returns:
            Nothing
        """

        # Get the cleartext code of the evaluate method
        cleartext_code = inspect.getsource(self.evaluate)

        # Remove docstrings
        cleartext_code = self.__remove_docstrings(cleartext_code)

        # Remove self parameter
        cleartext_code = re.sub(r"self,", "", cleartext_code)

        # Split the code into lines
        raw_lines = cleartext_code.split("\n")

        # Remove empty lines
        code_lines = [line for line in raw_lines if line.strip()]

        # Count the number of leading spaces of the first line
        leading_spaces = len(code_lines[0]) - len(code_lines[0].lstrip())

        # Remove the first leading spaces from all the lines
        code_lines = [line[leading_spaces:] for line in code_lines]

        # Combine the code lines into a single string
        reassembled_code = "\n".join(code_lines)

        # Remove references to self
        reassembled_code = self.__remove_self_references(reassembled_code)

        # Account for shift and noise
        reassembled_code = self.__decouple_shift_noise(reassembled_code)

        # Save the decoupled evaluate method to a file
        with open(DECOUPLED_FUNCTION_PATH, "w") as file_write:

            with open(__file__, "r") as file_read:

                # Import all the libraries used in the objective_function.py file
                for line in file_read.readlines():
                    if line.startswith("def "):
                        break
                    elif "import" in line:
                        file_write.write(f"{line.strip()}\n")

            # Write the decoupled evaluate method
            file_write.write(reassembled_code)

    @staticmethod
    def __delete_decoupled_evaluate() -> None:
        """
        Deletes the decoupled evaluate method file.

        Args:
            None

        Returns:
            Nothing
        """
        if os.path.exists(DECOUPLED_FUNCTION_PATH):
            os.remove(DECOUPLED_FUNCTION_PATH)

    def __call__(self, position: np.ndarray) -> float:
        """
        Evaluates the objective function at the given position.

        Args:
            position (np.ndarray): The position at which to evaluate the objective function.

        Returns:
            float: The value of the objective function at the given position.
        """
        return self.evaluate(position)

    def profile(
        self, nb_calls: int = 100, nb_positions: int = 10000, filename: str = ""
    ) -> None:
        """
        Profiles the ObjectiveFunction.evaluate method.

        Args:
            nb_calls (int, optional): The number of times to call the evaluate method. Defaults to 100.
            nb_positions (int, optional): The number of positions to evaluate at each call. Defaults to 10E4.

        Returns:
            Nothing
        """

        # Generate random positions to evaluate
        position = np.array(
            [
                np.array(
                    [
                        np.random.uniform(low, high)
                        for low, high in self.search_space_bounds
                    ]
                )
                for _ in range(nb_positions)
            ]
        )

        # Wrap the evaluate method
        def evaluate_wrapper():
            for _ in range(nb_calls):
                self.evaluate(position)

        # Print the profiling message
        logger.info("\nLine-by-line profiling of the .evaluate() method:\n")

        # Create an instance of LineProfiler and add the evaluate method to it
        profiler = LineProfiler()
        profiler.add_function(self.evaluate)

        # Run the profiler on the wrapped method
        profiler_wrapper = profiler(evaluate_wrapper)
        profiler_wrapper()

        # Print the results
        profiler.print_stats()

        # Print the results to a file
        if filename:
            with open(filename, "w") as file:
                profiler.print_stats(file)
